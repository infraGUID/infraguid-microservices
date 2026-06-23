import os
from functools import lru_cache
from typing import Optional

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_env: str = Field(default="development", alias="APP_ENV")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    frontend_origin: str = Field(default="http://localhost:3000", alias="FRONTEND_ORIGIN")
    backend_host: str = Field(default="0.0.0.0", alias="BACKEND_HOST")
    backend_port: int = Field(default=8000, alias="BACKEND_PORT")

    aws_secret_name: str = Field(default="", alias="AWS_SECRET_NAME")

    postgres_uri: str = Field(
        default="postgresql+asyncpg://postgres:postgres@localhost:5432/infraguid",
        alias="POSTGRES_URI",
    )

    s3_document_bucket: str = Field(default="infraguidai-documents", alias="S3_DOCUMENT_BUCKET")

    knowledge_base_path: str = Field(default="/app/knowledge-base", alias="KNOWLEDGE_BASE_PATH")
    chunk_size: int = Field(default=1000, alias="CHUNK_SIZE")
    chunk_overlap: int = Field(default=200, alias="CHUNK_OVERLAP")
    rag_top_k: int = Field(default=5, alias="RAG_TOP_K")

    aws_region: str = Field(default="us-east-1", alias="AWS_REGION")
    aws_access_key_id: Optional[str] = Field(default=None, alias="AWS_ACCESS_KEY_ID")
    aws_secret_access_key: Optional[str] = Field(default=None, alias="AWS_SECRET_ACCESS_KEY")
    aws_session_token: Optional[str] = Field(default=None, alias="AWS_SESSION_TOKEN")
    bedrock_chat_model_id: str = Field(default="amazon.nova-pro-v1:0", alias="BEDROCK_CHAT_MODEL_ID")
    bedrock_embedding_model_id: str = Field(default="amazon.titan-embed-text-v2:0", alias="BEDROCK_EMBEDDING_MODEL_ID")

    cognito_user_pool_id: str = Field(default="", alias="COGNITO_USER_POOL_ID")
    cognito_app_client_id: str = Field(default="", alias="COGNITO_APP_CLIENT_ID")
    cognito_region: str = Field(default="us-east-1", alias="COGNITO_REGION")

    agent_service_url: str = Field(default="http://localhost:8001", alias="AGENT_SERVICE_URL")
    rag_service_url: str = Field(default="http://localhost:8002", alias="RAG_SERVICE_URL")
    ingestion_service_url: str = Field(default="http://localhost:8003", alias="INGESTION_SERVICE_URL")

    redis_url: str = Field(default="redis://localhost:6379/0", alias="REDIS_URL")
    session_ttl_seconds: int = Field(default=7200, alias="SESSION_TTL_SECONDS")  # 2 hours

    sqs_ingestion_queue_url: str = Field(default="", alias="SQS_INGESTION_QUEUE_URL")
    ingestion_worker_enabled: bool = Field(default=True, alias="INGESTION_WORKER_ENABLED")
    sqs_wait_time_seconds: int = Field(default=20, alias="SQS_WAIT_TIME_SECONDS")
    sqs_visibility_timeout: int = Field(default=900, alias="SQS_VISIBILITY_TIMEOUT")  # 15 min

    @property
    def sqs_ingestion_enabled(self) -> bool:
        return bool(self.sqs_ingestion_queue_url)

    @property
    def bedrock_configured(self) -> bool:
        has_explicit_credentials = bool(self.aws_access_key_id and self.aws_secret_access_key)
        has_region_configured = bool(self.aws_region)
        return has_explicit_credentials or has_region_configured

    @property
    def cognito_configured(self) -> bool:
        return bool(self.cognito_user_pool_id and self.cognito_app_client_id)

    @property
    def secrets_manager_enabled(self) -> bool:
        return bool(self.aws_secret_name)

@lru_cache
def get_settings() -> Settings:
    from infraguid_common.config.secrets_manager import load_secrets_into_env

    load_secrets_into_env()
    return Settings()
