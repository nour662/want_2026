import os
from pathlib import Path
from typing import BinaryIO, Optional
import boto3
from botocore.exceptions import ClientError


class StorageBackend:
    def save_file(self, file_path: Path, content: bytes) -> Path:
        raise NotImplementedError

    def read_file(self, file_path: Path) -> bytes:
        raise NotImplementedError

    def delete_file(self, file_path: Path) -> bool:
        raise NotImplementedError

    def exists(self, file_path: Path) -> bool:
        raise NotImplementedError


class LocalStorage(StorageBackend):
    def __init__(self, base_dir: Path = Path("./sam_extracts")):
        self.base_dir = base_dir
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def save_file(self, file_path: Path, content: bytes) -> Path:
        full_path = self.base_dir / file_path
        full_path.parent.mkdir(parents=True, exist_ok=True)
        with open(full_path, "wb") as f:
            f.write(content)
        return full_path

    def read_file(self, file_path: Path) -> bytes:
        full_path = self.base_dir / file_path
        with open(full_path, "rb") as f:
            return f.read()

    def delete_file(self, file_path: Path) -> bool:
        full_path = self.base_dir / file_path
        if full_path.exists():
            full_path.unlink()
            return True
        return False

    def exists(self, file_path: Path) -> bool:
        return (self.base_dir / file_path).exists()


class S3Storage(StorageBackend):
    def __init__(self, bucket_name: str, prefix: str = "sam_extracts"):
        self.bucket_name = bucket_name
        self.prefix = prefix
        self.s3_client = boto3.client('s3')

    def _get_key(self, file_path: Path) -> str:
        return f"{self.prefix}/{file_path}"

    def save_file(self, file_path: Path, content: bytes) -> Path:
        key = self._get_key(file_path)
        self.s3_client.put_object(Bucket=self.bucket_name, Key=key, Body=content)
        return Path(key)

    def read_file(self, file_path: Path) -> bytes:
        key = self._get_key(file_path)
        response = self.s3_client.get_object(Bucket=self.bucket_name, Key=key)
        return response['Body'].read()

    def delete_file(self, file_path: Path) -> bool:
        key = self._get_key(file_path)
        try:
            self.s3_client.delete_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError:
            return False

    def exists(self, file_path: Path) -> bool:
        key = self._get_key(file_path)
        try:
            self.s3_client.head_object(Bucket=self.bucket_name, Key=key)
            return True
        except ClientError:
            return False


def get_storage() -> StorageBackend:
    storage_type = os.getenv("STORAGE_TYPE", "local")

    if storage_type == "s3":
        bucket_name = os.getenv("S3_BUCKET_NAME")
        if not bucket_name:
            raise ValueError("S3_BUCKET_NAME environment variable required for S3 storage")
        return S3Storage(bucket_name)

    return LocalStorage()
