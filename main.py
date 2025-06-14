# -*- coding: utf-8 -*-
import os
import json
import time
import boto3
import requests
import datetime
import langchain
import uuid
import logging
import asyncio
import pandas as pd

from opensearchpy import OpenSearch
from langchain.text_splitter import RecursiveCharacterTextSplitter
from langchain_community.vectorstores import OpenSearchVectorSearch
from langchain.chains import (
    create_history_aware_retriever,
    create_retrieval_chain
)
from langchain.chains.combine_documents import create_stuff_documents_chain
from langchain_core.prompts import ChatPromptTemplate, MessagesPlaceholder
from langchain_core.documents import Document
from langchain_core.load import dumpd, dumps, load, loads
from langchain_community.document_loaders import S3FileLoader
from langchain_community.llms import YandexGPT
from langchain_community.embeddings.yandex import YandexGPTEmbeddings
from langchain_core.messages import HumanMessage, AIMessage
from langchain_core.retrievers import BaseRetriever
from langchain_core.callbacks import CallbackManagerForRetrieverRun

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackQueryHandler
)

# Import configurations from config.py
try:
    import config
except ImportError:
    print("Error: config.py not found. Please ensure it's in the same directory.")
    exit(1)

# --- Logging Setup ---
logging.basicConfig(
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# --- Global Variables ---
user_chat_histories = {}
ragas_data_pool = [] # For potential future use

# --- Credential Loading ---
def load_json_file(filename, quiet=False):
    try:
        with open(filename, 'r', encoding='utf-8') as f:
            return json.load(f)
    except FileNotFoundError:
        if not quiet:
            logger.warning(f"JSON file '{filename}' not found.")
        return {}
    except json.JSONDecodeError:
        if not quiet:
            logger.warning(f"Could not decode JSON from '{filename}'.")
        return {}

# Load credentials
llm_creds = load_json_file(config.LLM_CRED_FILE)
opensearch_creds = load_json_file(config.OPENSEARCH_CRED_FILE)
s3_creds = load_json_file(config.S3_CRED_FILE)
telegram_creds = load_json_file(config.TELEGRAM_CRED_FILE)

LLM_SECRET_KEY = llm_creds.get('api_key')
FOLDER_ID = llm_creds.get('folder_id')

DB_USER = opensearch_creds.get('db_user')
DB_PASS = opensearch_creds.get('db_password')
DB_HOSTS = opensearch_creds.get('db_hosts') # This should be a single host URL string for the client

S3_BUCKET = s3_creds.get('bucket')
S3_BUCKET_PREFIX = s3_creds.get('bucket_prefix', '') # Default to empty string if not present
S3_KEY_ID = s3_creds.get('aws_access_key_id')
S3_SECRET_KEY = s3_creds.get('aws_secret_access_key')
S3_ENDPOINT_URL = s3_creds.get('endpoint_url')

TELEGRAM_BOT_TOKEN = telegram_creds.get('tg_token')

# --- S3 Client Initialization ---
s3_client = None
if S3_KEY_ID and S3_SECRET_KEY and S3_ENDPOINT_URL and S3_BUCKET:
    try:
        session = boto3.session.Session()
        s3_client = session.client(
            service_name='s3',
            aws_access_key_id=S3_KEY_ID,
            aws_secret_access_key=S3_SECRET_KEY,
            endpoint_url=S3_ENDPOINT_URL
        )
        logger.info("S3 client initialized successfully.")
    except Exception as e:
        logger.error(f"Failed to initialize S3 client: {e}")
        s3_client = None # Ensure it's None if initialization fails
else:
    logger.warning("S3 credentials or bucket information missing. S3 operations will not be available.")


# --- LLM and Embeddings Initialization ---
llm = None
embeddings = None
if LLM_SECRET_KEY and FOLDER_ID:
    try:
        llm = YandexGPT(
            model_name=config.MODEL_NAME,
            api_key=LLM_SECRET_KEY,
            folder_id=FOLDER_ID,
            temperature=config.LLM_TEMP,
            max_tokens=config.MAX_TOKENS
        )
        embeddings = YandexGPTEmbeddings(
            folder_id=FOLDER_ID,
            api_key=LLM_SECRET_KEY,
            sleep_interval=0.1
        )
        logger.info("YandexGPT LLM and Embeddings initialized.")
    except Exception as e:
        logger.error(f"Error initializing YandexGPT LLM or Embeddings: {e}")
else:
    logger.error("LLM Secret Key or Folder ID is missing. LLM and Embeddings not initialized.")


# --- Helper Function for LLM Calls (Metadata Extraction) ---
def ask_llm_for_metadata(prompt_content, instruction_text):
    if not llm:
        logger.error("LLM not initialized, cannot ask for metadata.")
        return ""

    full_prompt_text = f"{instruction_text}\\n\\n{prompt_content}"
    headers = {
        'Authorization': f'Api-Key {LLM_SECRET_KEY}',
        'Content-Type': 'application/json',
        'x-folder-id': FOLDER_ID
    }
    body = {
        "modelUri": f"gpt://{FOLDER_ID}/{config.MODEL_NAME}/latest",
        "completionOptions": {
            "temperature": config.LLM_TEMP, # Use low temp for factual extraction
            "maxTokens": 200 # Reduced for metadata
        },
        "messages": [{"role": "user", "text": full_prompt_text}]
    }
    try:
        response = requests.post(
            "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
            headers=headers, json=body, timeout=90
        )
        response.raise_for_status()
        result = response.json()
        alternatives = result.get('result', {}).get('alternatives', [])
        if alternatives and isinstance(alternatives, list) and alternatives[0].get('message'):
            return alternatives[0].get('message', {}).get('text', "").strip()
        logger.warning(f"LLM response structure unexpected for metadata: {result}")
        return ""
    except requests.exceptions.RequestException as e:
        logger.error(f"LLM request for metadata failed: {e}")
        return ""
    except (KeyError, IndexError, AttributeError) as e:
        logger.error(f"Error parsing LLM metadata response: {e}. Response: {result if 'result' in locals() else 'No response object'}")
        return ""

# --- Document Processing ---
def docs_from_s3_files(files_to_process):
    processed_docs = []
    if not s3_client or not S3_BUCKET:
        logger.error("S3 client or bucket not configured. Cannot process files from S3.")
        return processed_docs

    for file_path in files_to_process:
        logger.info(f"Processing S3 file: {file_path}")
        try:
            loaded_content = S3FileLoader(
                S3_BUCKET,
                file_path,
                aws_access_key_id=S3_KEY_ID,
                aws_secret_access_key=S3_SECRET_KEY,
                endpoint_url=S3_ENDPOINT_URL
            ).load()

            for doc_item in loaded_content:
                page_content_for_prompt = doc_item.page_content[:1800] # Limit prompt size
                instruction_text = (
                    "Extract the title of the document and summarize the main topics. "
                    "Respond in JSON format with keys \"title\" (string) and \"topics\" (string, comma-separated). "
                    "Example: {\"title\": \"Document Name\", \"topics\": \"topic1, topic2, topic3\"}. "
                    "If extraction fails, use {\"title\": \"Default Title\", \"topics\": \"not defined\"}."
                )
                metadata_json_str = ask_llm_for_metadata(page_content_for_prompt, instruction_text)
                metadata_dict = {}
                if metadata_json_str:
                    try:
                        clean_json_str = metadata_json_str.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                        metadata_dict = json.loads(clean_json_str)
                        logger.info(f"Extracted metadata for {file_path}: {metadata_dict}")
                    except json.JSONDecodeError:
                        logger.warning(f"Failed to parse LLM JSON metadata for {file_path}: '{metadata_json_str}'")
                        metadata_dict = {"title": f"Metadata error for {file_path.split('/')[-1]}", "topics": "not defined"}
                else:
                    metadata_dict = {"title": f"No LLM metadata for {file_path.split('/')[-1]}", "topics": "not defined"}

                doc_item.metadata['title'] = metadata_dict.get('title', f"File {file_path.split('/')[-1]}")
                doc_item.metadata['topics'] = metadata_dict.get('topics', "not defined")
                doc_item.metadata['source_file_key'] = file_path
                processed_docs.append(doc_item)
        except Exception as e:
            logger.error(f"Failed to process S3 file {file_path}: {e}")
            processed_docs.append(Document(
                page_content=f"Error processing content from {file_path}. Error: {e}",
                metadata={"source": file_path, "title": "Error Document", "topics": "error", "source_file_key": file_path}
            ))
    return processed_docs

def get_documents():
    docs = []
    # Check if serialized documents exist and load them
    if not config.FORCE_PROCESS_DOCS_FROM_S3 and os.path.exists(config.SERIALIZED_DOCS_FILE):
        try:
            with open(config.SERIALIZED_DOCS_FILE, 'r', encoding='utf-8') as f:
                string_representation = f.read()
                docs = loads(string_representation)
            logger.info(f"Loaded {len(docs)} documents from {config.SERIALIZED_DOCS_FILE}")
            if not docs: # File might be empty
                 logger.warning(f"{config.SERIALIZED_DOCS_FILE} was empty. Will try to process from S3.")
                 raise FileNotFoundError # Trigger S3 processing
        except Exception as e:
            logger.warning(f"Error loading documents from {config.SERIALIZED_DOCS_FILE}: {e}. Will try to process from S3.")
            docs = [] # Ensure docs is empty to trigger S3 processing

    if not docs: # If docs are still empty (not loaded from file or force process)
        if not s3_client or not S3_BUCKET or not S3_BUCKET_PREFIX:
            logger.error("S3 client, bucket or prefix not configured. Cannot fetch files from S3.")
            return []
        logger.info("Processing documents from S3...")
        try:
            s3_objects = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix=S3_BUCKET_PREFIX)
            all_files = [item['Key'] for item in s3_objects.get('Contents', [])]
            rag_files = [x for x in all_files if not x.endswith('/') and '.ipynb_checkpoints' not in x]
            logger.info(f"Found {len(rag_files)} files in S3 to process.")

            if not rag_files:
                logger.warning(f"No files found in S3 bucket '{S3_BUCKET}' with prefix '{S3_BUCKET_PREFIX}'.")
                return []

            docs = docs_from_s3_files(rag_files)
            if docs:
                try:
                    string_representation = dumps(docs, pretty=True)
                    with open(config.SERIALIZED_DOCS_FILE, 'w', encoding='utf-8') as f:
                        f.write(string_representation)
                    logger.info(f"Processed and saved {len(docs)} documents to {config.SERIALIZED_DOCS_FILE}")
                except Exception as e:
                    logger.error(f"Error saving serialized documents: {e}")
            else:
                logger.warning("No documents were processed from S3.")
        except Exception as e:
            logger.error(f"Error listing or processing files from S3: {e}")
            return []

    if not docs:
        logger.critical("No documents available for the RAG system. Bot may not function correctly.")
    return docs

# --- Text Splitter ---
text_splitter = RecursiveCharacterTextSplitter(
    separators=['\\n\\n', '\\n', ' ', '.', ','],
    chunk_size=config.CHUNK_SIZE,
    chunk_overlap=config.CHUNK_OVERLAP
)

# --- Vector Store Setup ---
vectorstore = None
def initialize_vectorstore(documents_to_index):
    global vectorstore
    if not config.OPENSEARCH_ENABLED:
        logger.info("OpenSearch is disabled in config. Skipping vector store initialization.")
        return None
    if not embeddings:
        logger.error("Embeddings not initialized. Cannot initialize OpenSearchVectorSearch.")
        return None
    if not DB_HOSTS or not DB_USER or not DB_PASS:
        logger.error("OpenSearch credentials or host missing. Cannot initialize vector store.")
        return None

    opensearch_url = DB_HOSTS # Should be a single URL like "https://user:pass@host:port" or use http_auth
    http_auth_creds = (DB_USER, DB_PASS) 

    verify_certs_os = True
    actual_ca_certs_path = config.CA_CERT_PATH
    if not os.path.exists(actual_ca_certs_path):
        logger.warning(f"CA certificate file not found at {actual_ca_certs_path}. Trying connection without specific CA.")

    try:
        # Check if the OpenSearch client can connect
        os_client_test = OpenSearch(
            [opensearch_url],
            http_auth=http_auth_creds,
            use_ssl=True,
            verify_certs=verify_certs_os,
            ca_certs=actual_ca_certs_path if os.path.exists(actual_ca_certs_path) else None,
            timeout=30
        )
        if not os_client_test.ping():
            logger.error("Failed to ping OpenSearch. Check connection and credentials.")
            return None
        logger.info(f"OpenSearch ping successful. Client info: {os_client_test.info().get('version')}")

        index_exists = os_client_test.indices.exists(index=config.OS_INDEX_NAME)

        if documents_to_index and (not index_exists or config.FORCE_PROCESS_DOCS_FROM_S3): # If forcing or index doesn't exist
            if index_exists and config.FORCE_PROCESS_DOCS_FROM_S3:
                logger.info(f"FORCE_PROCESS_DOCS_FROM_S3 is True. Deleting existing index: {config.OS_INDEX_NAME}")
                os_client_test.indices.delete(index=config.OS_INDEX_NAME)

            logger.info(f"Creating and populating index {config.OS_INDEX_NAME} in OpenSearch.")
            vectorstore = OpenSearchVectorSearch.from_documents(
                documents_to_index,
                embeddings,
                index_name=config.OS_INDEX_NAME,
                opensearch_url=opensearch_url,
                http_auth=http_auth_creds,
                use_ssl=True,
                verify_certs=verify_certs_os,
                ca_certs=actual_ca_certs_path if os.path.exists(actual_ca_certs_path) else None,
                engine="lucene",
                bulk_size=config.BULK_SIZE,
                hybrid_search=True
            )
            logger.info(f"Vectorstore populated in OpenSearch index '{config.OS_INDEX_NAME}'.")
        elif index_exists:
            logger.info(f"Connecting to existing OpenSearch index: {config.OS_INDEX_NAME}")
            vectorstore = OpenSearchVectorSearch(
                embedding_function=embeddings,
                index_name=config.OS_INDEX_NAME,
                opensearch_url=opensearch_url,
                http_auth=http_auth_creds,
                use_ssl=True,
                verify_certs=verify_certs_os,
                ca_certs=actual_ca_certs_path if os.path.exists(actual_ca_certs_path) else None,
                engine="lucene",
                hybrid_search=True
            )
        else:
            logger.error(f"OpenSearch index '{config.OS_INDEX_NAME}' does not exist and no documents provided to create it.")
            return None

        if vectorstore:
             vectorstore.is_hybrid_search = True
             logger.info("OpenSearch vectorstore initialized and configured for hybrid search.")
        return vectorstore

    except Exception as e:
        logger.error(f"Error initializing OpenSearch vector store: {e}", exc_info=True)
        return None


# --- Custom Hybrid Search Retriever ---
class OpenSearchHybridSearchRetriever(BaseRetriever):
    vectorstore: OpenSearchVectorSearch
    k: int = config.K_MAX
    search_kwargs: dict = {
        "fusion_algorithm": "rrf",
        "vector_search_weight": 0.5,
        "keyword_search_weight": 0.5,
    }

    def _get_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        try:
            # Ensure is_hybrid_search is True on the vectorstore instance
            if not getattr(self.vectorstore, 'is_hybrid_search', False):
                 self.vectorstore.is_hybrid_search = True
                 logger.warning("Retriever forced is_hybrid_search=True on vectorstore.")

            results_with_scores: list[tuple[Document, float]] = self.vectorstore.similarity_search_with_score(
                query=query,
                k=self.k,
                **self.search_kwargs
            )
            return [doc for doc, score in results_with_scores]
        except Exception as e:
            logger.error(f"Error in OpenSearchHybridSearchRetriever _get_relevant_documents: {e}", exc_info=True)
            return []

    async def _aget_relevant_documents(
        self, query: str, *, run_manager: CallbackManagerForRetrieverRun
    ) -> list[Document]:
        try:
            if not getattr(self.vectorstore, 'is_hybrid_search', False):
                 self.vectorstore.is_hybrid_search = True
                 logger.warning("Retriever (async) forced is_hybrid_search=True on vectorstore.")

            if hasattr(self.vectorstore, "asimilarity_search_with_score"):
                results_with_scores = await self.vectorstore.asimilarity_search_with_score(
                    query=query,
                    k=self.k,
                    **self.search_kwargs
                )
                return [doc for doc, score in results_with_scores]
            else:
                logger.warning("asimilarity_search_with_score not found, using sync version in executor.")
                loop = asyncio.get_event_loop()
                results_with_scores = await loop.run_in_executor(
                    None,
                    lambda q, k, sk: self.vectorstore.similarity_search_with_score(query=q, k=k, **sk),
                    query,
                    self.k,
                    self.search_kwargs
                )
                return [doc for doc, score in results_with_scores]
        except Exception as e:
            logger.error(f"Error in OpenSearchHybridSearchRetriever _aget_relevant_documents: {e}", exc_info=True)
            return []

# --- RAG Chain Setup ---
rag_chain = None
def initialize_rag_chain(current_vectorstore):
    global rag_chain
    if not current_vectorstore:
        logger.error("Vectorstore not available. Cannot initialize RAG chain.")
        return None
    if not llm:
        logger.error("LLM not initialized. Cannot initialize RAG chain.")
        return None

    retriever = OpenSearchHybridSearchRetriever(vectorstore=current_vectorstore, k=config.K_MAX)
    logger.info(f"OpenSearchHybridSearchRetriever initialized with K_MAX={config.K_MAX}.")

    contextualize_q_system_prompt = (
        "Given a chat history and the latest user question "
        "which might reference context in the chat history, "
        "formulate a standalone question which can be understood "
        "without the chat history. Do NOT answer the question, "
        "just reformulate it if needed and otherwise return it as is."
    )
    contextualize_q_prompt = ChatPromptTemplate.from_messages([
        ('system', contextualize_q_system_prompt),
        MessagesPlaceholder('chat_history'),
        ('human', '{input}'),
    ])
    history_aware_retriever = create_history_aware_retriever(llm, retriever, contextualize_q_prompt)

    qa_system_prompt = (
        "You are an administrative assistant for students of the Master in Business Analytics and Big Data (MiBA)"
        "program at SPbU GSOM."
        "Your role is to answer questions strictly based on the provided context from university documents."
        "Do not use any external knowledge or make assumptions beyond what is written in the context."
    
        "Follow these instructions carefully:"
        "1.  Thoroughly read the user's question and the provided context."
        "2.  Formulate a concise and direct answer using ONLY the information found in the context."
        "3.  If the answer to the question is explicitly stated in the context, provide it."
        "4.  If the answer cannot be found in the provided context, you MUST explicitly state: "Based on my knowledge" 
        "database, I could not find specific information about that. Please, push the "Help" button and contact the Office.""
        "5.  If multiple pieces of context are relevant, synthesize them into a coherent answer."
        "6.  Maintain a helpful and professional tone.\n\n"
        "Context:\n"
        "-----\n"
        "{context}\n"
        "-----\n"
        "Question: {input}\n"
        "Answer:"
    )
    qa_prompt = ChatPromptTemplate.from_messages([
        ('system', qa_system_prompt),
        MessagesPlaceholder('chat_history'),
        ('human', '{input}')
    ])
    question_answer_chain = create_stuff_documents_chain(llm, qa_prompt)
    rag_chain = create_retrieval_chain(history_aware_retriever, question_answer_chain)
    logger.info("RAG chain initialized successfully.")
    return rag_chain

# --- Query Preprocessing ---
def preprocess_query_for_retrieval(user_query: str):
    if not llm:
        logger.warning("LLM not initialized, skipping LLM-based query preprocessing.")
        return user_query

    logger.info(f"Original Query for preprocessing: {user_query}")
    # Step 1: Basic correction (optional, can be intensive)
    corrected_query = user_query # Placeholder if no correction step

    # Step 2: Translation and merging
    is_russian = any(c in "абвгдеёжзийклмнопрстуфхцчшщъыьэюя" for c in corrected_query.lower())
    translated_text = ""

    # LLM call for translation
    translation_instruction = ""
    if is_russian:
        translation_instruction = "Translate the following Russian text to English. Output only the translation: "
    else:
        translation_instruction = "Translate the following English text to Russian. Output only the translation: "

    translated_text = ask_llm_for_metadata(corrected_query, translation_instruction) # Reusing metadata LLM call structure

    final_query_for_retrieval = f"{corrected_query}, {translated_text}" if translated_text else corrected_query
    logger.info(f"Query after translation step: '{final_query_for_retrieval}'")

    # Step 3: Context-Aware Reformulation (Synonym Expansion)
    if config.SYNONYM_MAP:
        primary_lang_query_part = corrected_query
        appended_synonyms = set()
        words_in_primary = primary_lang_query_part.lower().split()

        for token in primary_lang_query_part.split():
            cleaned_token_lower = ''.join(filter(str.isalnum, token)).lower()
            for map_key, syn_list in config.SYNONYM_MAP.items():
                if map_key.lower() == cleaned_token_lower:
                    for syn in syn_list:
                        # Add synonym if it's not already in the primary query part or already added
                        if syn.lower() not in words_in_primary and syn.lower() not in (s.lower() for s in appended_synonyms):
                            appended_synonyms.add(syn)

        if appended_synonyms:
            # Append synonyms to the primary language part
            expanded_primary_query = f"{primary_lang_query_part} {' '.join(list(appended_synonyms))}"
            # Reconstruct final_query_for_retrieval with expanded primary and translated part
            if is_russian: # primary was Russian, translated was English
                final_query_for_retrieval = f"{expanded_primary_query}, {translated_text}" if translated_text else expanded_primary_query
            else: # primary was English, translated was Russian
                final_query_for_retrieval = f"{expanded_primary_query}, {translated_text}" if translated_text else expanded_primary_query
            logger.info(f"Query with appended synonyms: {final_query_for_retrieval}")

    if not final_query_for_retrieval.strip(): final_query_for_retrieval = user_query # Fallback
    logger.info(f"Final Preprocessed Query for RAG: {final_query_for_retrieval.strip()}")
    return final_query_for_retrieval.strip()


# --- S3 Presigned URL Generator ---
def generate_s3_presigned_url(object_key, expiration=3600):
    if not s3_client or not S3_BUCKET:
        logger.warning("S3 client or bucket not configured. Cannot generate presigned URL.")
        return None
    try:
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': S3_BUCKET, 'Key': object_key},
            ExpiresIn=expiration
        )
        return url
    except Exception as e:
        logger.error(f"Error generating presigned URL for {S3_BUCKET}/{object_key}: {e}")
        return None

# --- Telegram Bot Handlers ---
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    chat_id = update.effective_chat.id
    user_chat_histories[chat_id] = []
    if 'interactions' in context.chat_data:
        context.chat_data['interactions'].clear()
    await update.message.reply_html(
        rf"Hi! I am Mibi - an assistant bot - and I am ready to help you with information about MiBA program at GSOM SPbU, "
        "different additional materials and student opportunities. How can I help you?",
    )

async def handle_text_message(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat_id = update.effective_chat.id
    user_query_original = update.message.text

    if not rag_chain:
        await update.message.reply_text("I'm currently unable to access my knowledge base. Please try again later.")
        return

    if chat_id not in user_chat_histories:
        user_chat_histories[chat_id] = []
    current_chat_history = user_chat_histories[chat_id]

    processing_msg = await update.message.reply_text("🔄 Processing your request, please wait...")

    try:
        preprocessed_query = preprocess_query_for_retrieval(user_query_original)
    except Exception as e:
        logger.error(f"Query preprocessing failed for '{user_query_original}': {e}", exc_info=True)
        preprocessed_query = user_query_original
        await context.bot.edit_message_text("⚠️ Error in understanding query, using original.", chat_id=chat_id, message_id=processing_msg.message_id)

    try:
        response = await rag_chain.ainvoke({'input': preprocessed_query, 'chat_history': current_chat_history})
        answer = response.get('answer', "I couldn't find a specific answer based on the available information.")
        retrieved_contexts_docs = response.get('context', [])

        current_chat_history.extend([HumanMessage(content=user_query_original), AIMessage(content=answer)])
        user_chat_histories[chat_id] = current_chat_history[-20:] # Keep last 10 Q&A pairs

        await context.bot.delete_message(chat_id=chat_id, message_id=processing_msg.message_id)
        answer_message = await update.message.reply_text(answer)

        interaction_id = str(uuid.uuid4())
        if 'interactions' not in context.chat_data: context.chat_data['interactions'] = {}
        context.chat_data['interactions'][interaction_id] = {
            'question': user_query_original, 'preprocessed_question': preprocessed_query,
            'answer': answer, 'contexts_docs': retrieved_contexts_docs,
            'answer_message_id': answer_message.message_id, 'feedback': None,
            'timestamp': datetime.datetime.now().isoformat()
        }

        buttons_row1 = []
        if retrieved_contexts_docs:
            buttons_row1.append(InlineKeyboardButton("See the sources 📄", callback_data=f"sources_{interaction_id}"))

        buttons_row2 = [
            InlineKeyboardButton("👍", callback_data=f"feedback_positive_{interaction_id}"),
            InlineKeyboardButton("👎", callback_data=f"feedback_negative_{interaction_id}"),
            InlineKeyboardButton("Help", callback_data="action_show_help")
        ]

        keyboard_layout = []
        if buttons_row1: keyboard_layout.append(buttons_row1)
        keyboard_layout.append(buttons_row2)

        if keyboard_layout:
            reply_markup = InlineKeyboardMarkup(keyboard_layout)
            await update.message.reply_text("Was this helpful? You can also view sources or get help.", reply_markup=reply_markup)

        # Log for RAGAS data pool (optional for deployed bot, but can be useful)
        ragas_data_pool.append({
            'interaction_id': interaction_id, 'question': user_query_original,
            'preprocessed_question': preprocessed_query, 'answer': answer,
            'contexts': [doc.page_content for doc in retrieved_contexts_docs if isinstance(doc, Document)],
            'retrieved_document_sources_keys': [doc.metadata.get('source_file_key', doc.metadata.get('source', 'unknown')) for doc in retrieved_contexts_docs if isinstance(doc, Document)],
            'chat_id': chat_id, 'timestamp': context.chat_data['interactions'][interaction_id]['timestamp'],
            'feedback': None
        })

    except Exception as e:
        logger.error(f"Error handling message '{user_query_original}': {e}", exc_info=True)
        try:
            await context.bot.edit_message_text("An internal error occurred. Please try again later.", chat_id=chat_id, message_id=processing_msg.message_id)
        except:
            await update.message.reply_text("An internal error occurred. Please try again later.")

async def sources_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()

    try:
        _, interaction_id = query.data.split('_', 1)
    except ValueError:
        logger.warning(f"Malformed sources callback data: {query.data}")
        await query.edit_message_text(text="Error: Could not process this request.")
        return

    interaction_data = context.chat_data.get('interactions', {}).get(interaction_id)
    if not interaction_data or not interaction_data.get('contexts_docs'):
        await query.edit_message_text(text="Sorry, the sources for this answer are no longer available.")
        return

    sources_output_list = ["<b>Sources:</b>"]
    unique_s3_keys_info = {}
    for doc_obj in interaction_data['contexts_docs']:
        if isinstance(doc_obj, Document) and doc_obj.metadata:
            mtd = doc_obj.metadata
            title = mtd.get('title', 'Unknown Document')
            s3_key = mtd.get('source_file_key', mtd.get('source'))
            if s3_key and s3_key not in unique_s3_keys_info:
                presigned_url = generate_s3_presigned_url(s3_key)
                doc_filename = s3_key.split('/')[-1]
                entry = f"📄 <b>{title}</b> (File: {doc_filename})"
                if presigned_url: entry += f"\\n   <a href='{presigned_url}'>To see the document (link active for 1 hour)</a>"
                else: entry += "\\n   (Link unavailable)"
                unique_s3_keys_info[s3_key] = entry

    if not unique_s3_keys_info:
        sources_output_list.append("No specific document sources were identified.")
    else:
        sources_output_list.extend(f"{i+1}. {info}" for i, info in enumerate(unique_s3_keys_info.values()))

    final_text = "\\n\\n".join(sources_output_list)
    if len(final_text) > 4096: final_text = final_text[:4090] + "\\n...(list truncated)"

    try:
        await query.edit_message_text(text=final_text, parse_mode='HTML', disable_web_page_preview=True, reply_markup=None)
    except Exception as e:
        logger.error(f"Failed to edit message for sources: {e}. Sending new message.")
        answer_msg_id = interaction_data.get('answer_message_id')
        await context.bot.send_message(chat_id=query.message.chat_id, text=final_text,
                                       reply_to_message_id=answer_msg_id if answer_msg_id else None,
                                       parse_mode='HTML', disable_web_page_preview=True)

async def feedback_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    try:
        _, sentiment, interaction_id = query.data.split('_')
    except ValueError:
        logger.warning(f"Malformed feedback callback data: {query.data}")
        await query.edit_message_text(text="Error: Could not process feedback.")
        return

    feedback_recorded = False
    if 'interactions' in context.chat_data and interaction_id in context.chat_data['interactions']:
        context.chat_data['interactions'][interaction_id]['feedback'] = sentiment
        feedback_recorded = True
        # Update RAGAS pool if you're logging this
        for item in ragas_data_pool:
            if item.get('interaction_id') == interaction_id:
                item['feedback'] = sentiment
                break

    confirm_text = "Thanks for your feedback! 👍" if sentiment == "positive" else "Thanks for your feedback. We'll use this to improve. 👎"
    if not feedback_recorded: confirm_text = "Sorry, couldn't record feedback for this message."

    new_keyboard_layout = []
    current_keyboard = query.message.reply_markup.inline_keyboard if query.message.reply_markup else []
    for row_buttons in current_keyboard:
        temp_row = [btn for btn in row_buttons if btn.callback_data and (btn.callback_data.startswith("sources_") or btn.callback_data == "action_show_help")]
        if temp_row: new_keyboard_layout.append(temp_row)

    new_reply_markup = InlineKeyboardMarkup(new_keyboard_layout) if new_keyboard_layout else None

    try:
        await query.edit_message_text(
            text=f"{query.message.text}\\n\\n_{confirm_text}_",
            reply_markup=new_reply_markup,
            parse_mode='Markdown'
        )
    except Exception as e:
        logger.warning(f"Could not edit message for feedback confirmation: {e}. Sending new message.")
        await context.bot.send_message(chat_id=query.message.chat_id, text=confirm_text)

async def show_help_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    await context.bot.send_message(
        chat_id=query.message.chat_id,
        text=config.HELP_TEXT_CONTENT,
        parse_mode='HTML'
    )

# --- Main Bot Runner ---
def telegram_bot_runner():
    if not TELEGRAM_BOT_TOKEN:
        logger.critical("Telegram Bot Token is missing. Bot cannot start.")
        return
    if not rag_chain: # rag_chain is initialized after vectorstore
        logger.critical("RAG chain is not initialized. Core functionality will be missing. Bot will not start.")
        return

    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_message))
    application.add_handler(CallbackQueryHandler(sources_callback, pattern="^sources_"))
    application.add_handler(CallbackQueryHandler(feedback_callback, pattern="^feedback_"))
    application.add_handler(CallbackQueryHandler(show_help_callback, pattern="^action_show_help$"))

    logger.info("Telegram bot starting polling...")
    application.run_polling(drop_pending_updates=True)

# --- Main Execution ---
if __name__ == '__main__':
    # 1. Load documents (from S3 or local cache)
    documents = get_documents()

    if not documents:
        logger.critical("No documents loaded. Cannot proceed with vector store or RAG chain initialization. Bot will not start.")
        exit(1)

    # 2. Split documents
    docs_splitted = text_splitter.split_documents(documents)
    logger.info(f"Total chunks for vector store: {len(docs_splitted)}")

    if not docs_splitted:
        logger.critical("No document chunks to index. Bot will not start.")
        exit(1)

    # 3. Initialize Vector Store
    # This will also handle indexing if needed
    vs = initialize_vectorstore(docs_splitted)

    if not vs:
        logger.critical("Failed to initialize vector store. Bot cannot operate. Exiting.")
        exit(1)

    # 4. Initialize RAG Chain
    if not initialize_rag_chain(vs): # Pass the initialized vectorstore
        logger.critical("Failed to initialize RAG chain. Bot cannot operate. Exiting.")
        exit(1)

    # 5. Start the bot
    logger.info("All components initialized. Starting Telegram bot runner.")
    telegram_bot_runner()
