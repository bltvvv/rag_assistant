# Telegram RAG Bot for Student Administrative Information

This project implements a Telegram chatbot using the **Retrieval Augmented Generation (RAG)** approach to assist students of the Master in Business Analytics and Big Data (MiBA) program at SPbU GSOM with administrative information.

The bot leverages **YandexGPT** for language understanding and generation, **OpenSearch** for vector storage and retrieval, and **S3** for document storage.

---

## Features

- **RAG Architecture**: Answers questions based on a knowledge base of documents.
- **Telegram Integration**: Interacts with users via a Telegram interface.
- **Hybrid Search**: Utilizes OpenSearch for efficient semantic and keyword-based document retrieval.
- **Contextual Conversations**: Maintains chat history for follow-up questions.
- **Source Referencing**: Can provide links to the source documents used for answers.
- **Feedback Mechanism**: Allows users to provide feedback on answer helpfulness.
- **Query Preprocessing**: Includes synonym expansion and language translation (Russian/English) to improve retrieval.

---

## Project Structure

```

.
├── .opensearch/
│   └── root.crt                    # CA certificate for OpenSearch
├── main.py                         # Main application script for the bot
├── config.py                       # Configuration file for settings and paths
├── requirements.txt                # Python dependencies
├── api-credentials.json            # Credentials for YandexGPT (SAMPLE)
├── credentials\_opensearch.json    # Credentials for OpenSearch (SAMPLE)
├── accessbucket.json               # Credentials for Yandex S3 (SAMPLE)
├── tg-credentials.json             # Credentials for Telegram Bot (SAMPLE)
└── README.md                       # This file

````

---

## Prerequisites

- Python 3.9+
- A Yandex Cloud account (for S3, YandexGPT and OpenSearch)
- A Telegram Bot Token
- Access to an OpenSearch instance
- Access to a Yandex S3 bucket with your knowledge base documents

---

## Setup Instructions

### 1. Clone the Repository

```bash
git clone https://github.com/bltvvv/rag_assistant.git
````

Or ensure all the files listed in "Project Structure" are in your project directory.

### 2. Create Credential Files

Create the following JSON files in the root directory:

**api-credentials.json**

```json
{
    "api_key": "YOUR_YANDEX_GPT_API_KEY",
    "folder_id": "YOUR_YANDEX_CLOUD_FOLDER_ID"
}
```

**credentials\_opensearch.json**

```json
{
    "db_user": "YOUR_OPENSEARCH_USER",
    "db_password": "YOUR_OPENSEARCH_PASSWORD",
    "db_hosts": "YOUR_OPENSEARCH_HOST_URL"
}
```

> Example: `"db_hosts": "https://c-c9q...mdb.yandexcloud.net:9200"`

**accessbucket.json**

```json
{
    "bucket": "YOUR_S3_BUCKET_NAME",
    "bucket_prefix": "YOUR_S3_BUCKET_PREFIX_FOR_DOCS",
    "aws_access_key_id": "YOUR_S3_ACCESS_KEY_ID",
    "aws_secret_access_key": "YOUR_S3_SECRET_ACCESS_KEY",
    "endpoint_url": "https://storage.yandexcloud.net"
}
```

**tg-credentials.json**

```json
{
    "tg_token": "YOUR_TELEGRAM_BOT_TOKEN"
}
```

### 3. Place OpenSearch CA Certificate

Create a `.opensearch/` directory and place your `root.crt` file there (from Yandex Managed OpenSearch).

### 4. Set Up Python Virtual Environment (Recommended)

```bash
python3 -m venv venv
source venv/bin/activate   # On Windows: venv\Scripts\activate
```

### 5. Install Dependencies

```bash
pip install -r requirements.txt
```

---

## Configuration

Edit `config.py` to adjust:

* **Model settings**: `MODEL_NAME`, `LLM_TEMP`, `MAX_TOKENS`
* **OpenSearch**: `OS_INDEX_NAME`, `CA_CERT_PATH`, `CHUNK_SIZE`, `CHUNK_OVERLAP`, `K_MAX`
* **Paths to credentials**
* **Synonym map and language translation**
* **Reprocessing toggle**:

  ```python
  FORCE_PROCESS_DOCS_FROM_S3 = False
  ```

Set to `True` to re-download and re-process S3 documents.

---

## Running the Bot

### Initial Document Processing

On first run (or if `processed_langchain_docs.json` is missing or reprocessing is forced), the bot will:

* Connect to your Yandex S3 bucket
* Download and process documents using YandexGPT
* Save processed results to `processed_langchain_docs.json`
* Split content into chunks
* Store vectors in OpenSearch (index created if not existing)

> ⚠️ This can be slow and API-intensive.

---

### Running Locally

Ensure network access to OpenSearch and S3:

```bash
python3 main.py
```

Send messages to your Telegram bot and monitor console logs.

---

## Important Notes

### ⏳ First Run Duration

* Document processing, GPT calls, and indexing may take significant time initially.
* Later runs will be faster due to caching and indexed storage.

### 💾 Data Persistence

* `processed_langchain_docs.json`: speeds up future runs.

### 💸 Costs

* Monitor usage of Yandex services (GPT API, OpenSearch, S3).

---

