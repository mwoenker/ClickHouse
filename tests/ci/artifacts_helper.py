#!/usr/bin/env python

"""Manages artifacts similar to GH actions, but in S3"""

from dataclasses import dataclass
from fnmatch import fnmatch
from os import path as op
from pathlib import Path
from typing import List, Union

from github.Commit import Commit

from build_download_helper import download_build_with_progress
from commit_status_helper import post_commit_status
from compress_files import SUFFIX, compress_fast, decompress_fast
from env_helper import S3_BUILDS_BUCKET, S3_DOWNLOAD, TEMP_PATH
from git_helper import SHA_REGEXP
from report import HEAD_HTML_TEMPLATE, FOOTER_HTML_TEMPLATE
from s3_helper import S3Helper


@dataclass
class S3Object:
    key: str
    last_modified: str
    size: int


class ArtifactsHelper:
    INDEX = "index.html"
    RESTRICTED_SYMBOLS = r"\/':<>|*?\""

    def __init__(
        self,
        s3_helper: S3Helper,
        commit: Union[str, Commit],
        s3_prefix: str = "artifacts",
    ):
        """The helper to compress+upload and download+decompress artifacts
        If `commit` is github.Commit.Commit instance, the status Artifacts for a
        given commit will be updated on an uploading"""
        self._commit = commit
        assert SHA_REGEXP.match(self.commit)
        self.temp_path = Path(TEMP_PATH)
        self.s3_helper = s3_helper
        # The s3 prefix is done with trailing slash!
        self._s3_prefix = op.join(s3_prefix, self.commit, "")
        self._s3_index_key = f"{self.s3_prefix}{self.INDEX}"

    @property
    def commit(self) -> str:
        """string of the commit SHA"""
        if isinstance(self._commit, str):
            return self._commit
        return self._commit.sha

    @property
    def s3_prefix(self) -> str:
        """Prefix with the trailing slash"""
        return self._s3_prefix

    @property
    def s3_index_key(self) -> str:
        """Prefix with the trailing slash"""
        return self._s3_index_key

    def upload(self, artifact_name: str, artifact_path: Path) -> None:
        """Creates archive 'artifact_name.tar{compress_files.SUFFIX} with directory of"""
        assert not any(s in artifact_name for s in self.RESTRICTED_SYMBOLS)
        archive_path = self.temp_path / f"{artifact_name}.tar{SUFFIX}"
        s3_artifact_key = f"{self.s3_prefix}{archive_path.name}"
        compress_fast(artifact_path, archive_path)
        self.s3_helper.upload_build_file_to_s3(archive_path, s3_artifact_key)
        self._regenerate_index()

    def download(
        self, artifact_name: str, extract_directory: Path, keep_archive: bool = False
    ) -> bool:
        """Downloads artifact, if exists, and extracts it. If not, returns False"""
        assert not any(s in artifact_name for s in self.RESTRICTED_SYMBOLS)
        assert extract_directory.is_dir()
        archive_path = self.temp_path / f"{artifact_name}.tar{SUFFIX}"
        artifact_path = extract_directory / artifact_name
        s3_artifact_key = f"{self.s3_prefix}{archive_path.name}"
        if not self.s3_helper.exists(s3_artifact_key, S3_BUILDS_BUCKET):
            return False

        url = f"{S3_DOWNLOAD}/{S3_BUILDS_BUCKET}/{s3_artifact_key}"
        download_build_with_progress(url, archive_path)
        artifact_path.mkdir(parents=True, exist_ok=True)
        decompress_fast(archive_path, artifact_path)
        if not keep_archive:
            archive_path.unlink()
        return True

    def list_artifacts(self, glob: str = "") -> List[str]:
        """return the list of artifacts existing for a commit"""

        def ignore(key: str) -> bool:
            if key == self.s3_index_key:
                return False
            if glob:
                return fnmatch(key, glob)
            return True

        results = filter(
            ignore, self.s3_helper.list_prefix(self.s3_prefix, S3_BUILDS_BUCKET)
        )
        return list(results)

    def _regenerate_index(self) -> None:
        objects = self.s3_helper.client.list_objects_v2(
            Bucket=S3_BUILDS_BUCKET, Prefix=self.s3_prefix
        )
        files = []  # type: List[S3Object]
        links = []  # type: List[str]
        if "Contents" in objects:
            files = [
                S3Object(
                    obj["Key"][len(self.s3_prefix) :],
                    obj["LastModified"].isoformat(),
                    obj["Size"],
                )
                for obj in objects["Contents"]
            ]
            links = [
                f'<tr><td><a href="{f.key}">{f.key}</a></td><td>{f.size}</td>'
                f"<td>{f.last_modified}</td></tr>"
                for f in files
            ]
        index_path = self.temp_path / self.INDEX
        title = f"Artifacts for workflow commit {self.commit}"
        index_content = (
            HEAD_HTML_TEMPLATE.format(title=title, header=title)
            + "<table><tr><th>Artifact</th><th>Size</th><th>Modified</th></tr>"
            + "\n".join(links)
            + "</table>"
            + FOOTER_HTML_TEMPLATE
        )
        index_path.write_text(index_content, encoding="utf-8")
        url = self.s3_helper.upload_build_file_to_s3(index_path, self.s3_index_key)
        if isinstance(self._commit, Commit):
            post_commit_status(
                self._commit, "success", url, "Artifacts for workflow", "Artifacts"
            )
