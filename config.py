# -*- coding: utf-8 -*-

# LLM Configuration
MODEL_NAME = 'yandexgpt-lite'
LLM_TEMP = 0.0
MAX_TOKENS = 2000

# Vector Store Configuration
OPENSEARCH_ENABLED = True # Set to True to use OpenSearch, False for other/no vector store
OS_INDEX_NAME = 'miba-student-assist-hybrid-search' # OpenSearch index name
# Path to the OpenSearch CA certificate. Assumes root.crt is in a .opensearch subfolder.
CA_CERT_PATH = '.opensearch/root.crt'
CHUNK_SIZE = 1500 # Optimized chunk size
CHUNK_OVERLAP = 150 # Optimized chunk overlap
BULK_SIZE = 1000000 # For OpenSearch bulk indexing
K_MAX = 5 # Number of documents to retrieve

# File Paths
# Path for storing/loading processed Langchain documents
SERIALIZED_DOCS_FILE = 'processed_langchain_docs.json'

# Credential file names (these files should be in the same directory as main.py or provide full paths)
LLM_CRED_FILE = 'api-credentials.json'
OPENSEARCH_CRED_FILE = 'credentials_opensearch.json'
S3_CRED_FILE = 'accessbucket.json'
TELEGRAM_CRED_FILE = 'tg-credentials.json'

# Query Preprocessing
SYNONYM_MAP = {
    "course": ["program", "study plan", "module"], "courses": ["programs", "study plans", "modules"],
    "deadline": ["due date", "submission date"], "application": ["admission", "enrollment"],
    "GSOM": ["Graduate School of Management", "GSOM SPbU", "ВШМ"],
    "SPbU": ["Saint Petersburg State University", "СПбГУ"],
    "MiBA": ["Master in Business Analytics and Big Data", "Миба"],
    "ML": ["Machine Learning", "машинное обучение"], "AI": ["Artificial Intelligence", "искусственный интеллект"],
    "МЛ": ["Machine Learning", "машинное обучение"], "ИИ": ["Artificial Intelligence", "искусственный интеллект"],
    "exam": ["examination", "test", "assessment", "экзамен", "тест"],
    "schedule": ["timetable", "academic calendar", "расписание", "календарь"],
    "расписание": ["timetable", "academic calendar", "расписание", "календарь"],
    "обмен": ["включенное обучение", "included learning", "программа обмена", "outgoing", "exchange"],
    "exchange program": ["included learning", "включенное обучение"],
    "practice": ["practical training", "internship"],
    "практика": ["practical training", "internship"]
}

# Telegram Bot
HELP_TEXT_CONTENT = (
    "I can answer questions related to administrative information for MiBA students. "
    "Ask your question, and I'll do my best to assist you based on the available knowledge base.\\n\\n"
    "If you did not find an answer on your question, please contact the following people:\\n"
    "1. Name Surname, MiBA Program Assistant. His contacts: tg - @<tg_name>\\n"
    "2. Name Surname, GSOM Master Programs Manager. Her contacts: tg - @<tg_name>, mail - <mail>@gsom.spbu.ru\\n\\n"
)

# Document Processing
# Set to True to re-process documents from S3 even if SERIALIZED_DOCS_FILE exists.
# For normal bot operation, this should be False after initial setup.
FORCE_PROCESS_DOCS_FROM_S3 = False
