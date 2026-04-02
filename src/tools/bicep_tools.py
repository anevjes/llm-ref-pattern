import logging
import os
import re
from datetime import datetime, timezone
from typing import Annotated

from agent_framework import tool
from github import Auth, Github
from pydantic import Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Chunk manager — holds state for the chunked processing mode
# ---------------------------------------------------------------------------

class BicepChunkManager:
    """Manages Bicep file chunks for incremental processing."""

    def __init__(self) -> None:
        self.chunks: list[dict] = []          # {index, type, name, start_line, end_line, content}
        self.modified: dict[int, str] = {}    # index -> modified content
        self.source_repo: str = ""
        self.source_path: str = ""
        self.source_branch: str = ""

    def reset(self) -> None:
        self.chunks.clear()
        self.modified.clear()
        self.source_repo = ""
        self.source_path = ""
        self.source_branch = ""

    def parse(self, content: str) -> list[dict]:
        """Split Bicep content into logical sections."""
        lines = content.split("\n")
        chunks: list[dict] = []
        current_chunk: dict | None = None

        # Patterns for section boundaries
        section_pattern = re.compile(
            r"^(resource|param|var|output|module|targetScope)\b"
        )
        comment_header_pattern = re.compile(r"^// [-=]{3,}")

        i = 0
        while i < len(lines):
            line = lines[i]
            stripped = line.strip()

            # Detect section start
            section_match = section_pattern.match(stripped)
            if section_match:
                # Close previous chunk
                if current_chunk is not None:
                    current_chunk["end_line"] = i - 1
                    current_chunk["content"] = "\n".join(lines[current_chunk["start_line"]:i])
                    chunks.append(current_chunk)

                section_type = section_match.group(1)
                # Extract name for resources/params/vars/outputs/modules
                name = ""
                if section_type == "resource":
                    m = re.match(r"resource\s+(\w+)\s+", stripped)
                    name = m.group(1) if m else ""
                elif section_type in ("param", "var", "output", "module"):
                    m = re.match(rf"{section_type}\s+(\w+)\s+", stripped)
                    name = m.group(1) if m else ""

                current_chunk = {
                    "index": len(chunks),
                    "type": section_type,
                    "name": name,
                    "start_line": i,
                    "end_line": i,
                    "content": "",
                }
                i += 1
                continue

            # Detect comment headers (group consecutive comments/blanks as preamble)
            if current_chunk is None and (comment_header_pattern.match(stripped) or stripped.startswith("//") or stripped == ""):
                if not chunks or (chunks and chunks[-1]["type"] != "preamble"):
                    current_chunk = {
                        "index": len(chunks),
                        "type": "preamble",
                        "name": "header",
                        "start_line": i,
                        "end_line": i,
                        "content": "",
                    }
                i += 1
                continue

            i += 1

        # Close final chunk
        if current_chunk is not None:
            current_chunk["end_line"] = len(lines) - 1
            current_chunk["content"] = "\n".join(lines[current_chunk["start_line"]:])
            chunks.append(current_chunk)

        # Merge consecutive params, vars, outputs into group chunks
        self.chunks = self._merge_groups(chunks, lines)
        return self.chunks

    def _merge_groups(self, chunks: list[dict], lines: list[str]) -> list[dict]:
        """Merge consecutive param/var/output declarations into single chunks."""
        merged: list[dict] = []
        i = 0
        while i < len(chunks):
            c = chunks[i]
            if c["type"] in ("param", "var", "output"):
                group_type = c["type"]
                group_start = c["start_line"]
                group_names: list[str] = [c["name"]]
                j = i + 1
                while j < len(chunks) and chunks[j]["type"] == group_type:
                    group_names.append(chunks[j]["name"])
                    j += 1
                group_end = chunks[j - 1]["end_line"]
                merged.append({
                    "index": len(merged),
                    "type": f"{group_type}_group",
                    "name": f"{len(group_names)} {group_type}s: {', '.join(group_names[:5])}{'...' if len(group_names) > 5 else ''}",
                    "start_line": group_start,
                    "end_line": group_end,
                    "content": "\n".join(lines[group_start:group_end + 1]),
                })
                i = j
            else:
                c["index"] = len(merged)
                merged.append(c)
                i += 1
        return merged

    def get_summary(self) -> str:
        """Return a compact summary of all chunks (no content)."""
        lines = []
        for c in self.chunks:
            line_count = c["end_line"] - c["start_line"] + 1
            lines.append(
                f"  Chunk {c['index']}: [{c['type']}] {c['name']} "
                f"(lines {c['start_line']+1}-{c['end_line']+1}, {line_count} lines)"
            )
        return f"Total chunks: {len(self.chunks)}\n" + "\n".join(lines)

    def reassemble(self) -> str:
        """Reassemble all chunks, using modified content where available."""
        parts = []
        for c in self.chunks:
            if c["index"] in self.modified:
                parts.append(self.modified[c["index"]])
            else:
                parts.append(c["content"])
        return "\n".join(parts)


# Module-level chunk manager instance
_chunk_manager = BicepChunkManager()


def _get_github_client() -> Github:
    """Create an authenticated GitHub client from GITHUB_TOKEN env var."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        raise RuntimeError("GITHUB_TOKEN environment variable is not set.")
    return Github(auth=Auth.Token(token))


@tool(approval_mode="never_require")
def read_bicep_file(
    repo_name: Annotated[str, Field(description="The GitHub repository in 'owner/repo' format.")],
    file_path: Annotated[str, Field(description="The path to the Bicep file in the repository (e.g. 'infra/main.bicep').")],
    branch: Annotated[str, Field(description="The branch to read from.")] = "main",
) -> str:
    """Read a Bicep file from a remote GitHub repository and return its contents."""
    logger.info("[read_bicep_file] Reading from GitHub: %s/%s (branch: %s)", repo_name, file_path, branch)
    g = _get_github_client()
    try:
        repo = g.get_repo(repo_name)
        file_content = repo.get_contents(file_path, ref=branch)
        content = file_content.decoded_content.decode("utf-8")
        line_count = content.count("\n") + 1
        logger.info("[read_bicep_file] Successfully read %d characters (%d lines) from %s/%s (sha: %s)",
                     len(content), line_count, repo_name, file_path, file_content.sha)
        return content
    except Exception as e:
        logger.error("[read_bicep_file] Failed to read %s/%s from branch '%s': %s", repo_name, file_path, branch, e)
        return f"Error: Failed to read {file_path} from {repo_name} on branch {branch}: {e}"
    finally:
        g.close()


@tool(approval_mode="never_require")
def modify_bicep_file(
    repo_name: Annotated[str, Field(description="The GitHub repository in 'owner/repo' format.")],
    file_path: Annotated[str, Field(description="The original path of the Bicep file in the repository (e.g. 'infra/main.bicep').")],
    content: Annotated[str, Field(description="The full modified Bicep file content to write.")],
    branch: Annotated[str, Field(description="The branch to commit to.")] = "main",
    commit_message: Annotated[str, Field(description="The commit message.")] = "Update Bicep file via infra_prep agent",
) -> str:
    """Write modified Bicep content to a remote GitHub repository as a new file with _modified_<datetime> appended to the filename. Returns the GitHub URL."""
    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(file_path)
    target_path = f"{base}_modified_{now}{ext}"
    logger.info("[modify_bicep_file] Will write modified file to: %s/%s (branch: %s)", repo_name, target_path, branch)

    line_count = content.count("\n") + 1
    logger.info("[modify_bicep_file] Content to write: %d characters (%d lines)", len(content), line_count)

    g = _get_github_client()
    try:
        repo = g.get_repo(repo_name)
        logger.info("[modify_bicep_file] Connecting to GitHub repo: %s", repo_name)

        try:
            existing_file = repo.get_contents(target_path, ref=branch)
            logger.info("[modify_bicep_file] File already exists (sha: %s), updating...", existing_file.sha)
            result = repo.update_file(
                path=target_path,
                message=commit_message,
                content=content,
                sha=existing_file.sha,
                branch=branch,
            )
            logger.info("[modify_bicep_file] Successfully updated file: %s", result["content"].html_url)
            return f"File updated on GitHub: {result['content'].html_url}"
        except Exception:
            logger.info("[modify_bicep_file] Creating new file: %s", target_path)
            result = repo.create_file(
                path=target_path,
                message=commit_message,
                content=content,
                branch=branch,
            )
            logger.info("[modify_bicep_file] Successfully created new file: %s", result["content"].html_url)
            return f"File created on GitHub: {result['content'].html_url}"
    except Exception as e:
        logger.error("[modify_bicep_file] Failed to write to %s/%s: %s", repo_name, target_path, e)
        return f"Error: Failed to write modified file to {repo_name}/{target_path}: {e}"
    finally:
        g.close()
        logger.info("[modify_bicep_file] GitHub connection closed.")


# ---------------------------------------------------------------------------
# Chunked-mode tools
# ---------------------------------------------------------------------------

@tool(approval_mode="never_require")
def read_bicep_structure(
    repo_name: Annotated[str, Field(description="The GitHub repository in 'owner/repo' format.")],
    file_path: Annotated[str, Field(description="The path to the Bicep file in the repository (e.g. 'infra/main.bicep').")],
    branch: Annotated[str, Field(description="The branch to read from.")] = "main",
) -> str:
    """Read a Bicep file from GitHub, parse it into chunks, and return a structure summary (NOT the full content).
    Use get_bicep_chunk to retrieve individual chunk contents for modification."""
    logger.info("[read_bicep_structure] Reading from GitHub: %s/%s (branch: %s)", repo_name, file_path, branch)
    _chunk_manager.reset()
    _chunk_manager.source_repo = repo_name
    _chunk_manager.source_path = file_path
    _chunk_manager.source_branch = branch

    g = _get_github_client()
    try:
        repo = g.get_repo(repo_name)
        file_content = repo.get_contents(file_path, ref=branch)
        content = file_content.decoded_content.decode("utf-8")
        line_count = content.count("\n") + 1
        logger.info("[read_bicep_structure] Read %d characters (%d lines), parsing into chunks...",
                     len(content), line_count)

        _chunk_manager.parse(content)
        summary = _chunk_manager.get_summary()
        logger.info("[read_bicep_structure] Parsed into %d chunks", len(_chunk_manager.chunks))
        return (
            f"Bicep file: {repo_name}/{file_path} (branch: {branch})\n"
            f"Total size: {len(content)} characters, {line_count} lines\n\n"
            f"{summary}\n\n"
            f"Use get_bicep_chunk(index) to retrieve a chunk's content for modification.\n"
            f"Use update_bicep_chunk(index, content) to store your modified version.\n"
            f"When done, call commit_bicep_chunks() to reassemble and commit."
        )
    except Exception as e:
        logger.error("[read_bicep_structure] Failed: %s", e)
        return f"Error: Failed to read {file_path} from {repo_name}: {e}"
    finally:
        g.close()


@tool(approval_mode="never_require")
def get_bicep_chunk(
    index: Annotated[int, Field(description="The chunk index to retrieve (from read_bicep_structure output).")],
) -> str:
    """Get the full content of a specific Bicep chunk by index."""
    if not _chunk_manager.chunks:
        return "Error: No chunks loaded. Call read_bicep_structure first."
    if index < 0 or index >= len(_chunk_manager.chunks):
        return f"Error: Invalid chunk index {index}. Valid range: 0-{len(_chunk_manager.chunks) - 1}"

    chunk = _chunk_manager.chunks[index]
    logger.info("[get_bicep_chunk] Returning chunk %d: [%s] %s (%d lines)",
                 index, chunk["type"], chunk["name"],
                 chunk["end_line"] - chunk["start_line"] + 1)
    return (
        f"Chunk {index} [{chunk['type']}] {chunk['name']} "
        f"(lines {chunk['start_line']+1}-{chunk['end_line']+1}):\n\n"
        f"{chunk['content']}"
    )


@tool(approval_mode="never_require")
def update_bicep_chunk(
    index: Annotated[int, Field(description="The chunk index to update.")],
    content: Annotated[str, Field(description="The modified content for this chunk.")],
) -> str:
    """Store modified content for a specific chunk. Call commit_bicep_chunks when all modifications are done."""
    if not _chunk_manager.chunks:
        return "Error: No chunks loaded. Call read_bicep_structure first."
    if index < 0 or index >= len(_chunk_manager.chunks):
        return f"Error: Invalid chunk index {index}. Valid range: 0-{len(_chunk_manager.chunks) - 1}"

    chunk = _chunk_manager.chunks[index]
    old_lines = chunk["content"].count("\n") + 1
    new_lines = content.count("\n") + 1
    _chunk_manager.modified[index] = content
    logger.info("[update_bicep_chunk] Stored modified chunk %d: [%s] %s (%d -> %d lines, %d modified total)",
                 index, chunk["type"], chunk["name"], old_lines, new_lines,
                 len(_chunk_manager.modified))
    return (
        f"Chunk {index} updated ({old_lines} -> {new_lines} lines). "
        f"Modified chunks so far: {len(_chunk_manager.modified)}/{len(_chunk_manager.chunks)}. "
        f"Call commit_bicep_chunks when all modifications are done."
    )


@tool(approval_mode="never_require")
def commit_bicep_chunks(
    commit_message: Annotated[str, Field(description="The commit message.")] = "Update Bicep file via infra_prep agent (chunked)",
) -> str:
    """Reassemble all chunks (modified + unmodified), and commit to GitHub as a new file with _modified_<datetime>."""
    if not _chunk_manager.chunks:
        return "Error: No chunks loaded. Call read_bicep_structure first."
    if not _chunk_manager.modified:
        return "Error: No chunks have been modified. Nothing to commit."

    repo_name = _chunk_manager.source_repo
    file_path = _chunk_manager.source_path
    branch = _chunk_manager.source_branch

    logger.info("[commit_bicep_chunks] Reassembling %d chunks (%d modified)...",
                 len(_chunk_manager.chunks), len(_chunk_manager.modified))
    content = _chunk_manager.reassemble()
    line_count = content.count("\n") + 1
    logger.info("[commit_bicep_chunks] Reassembled file: %d characters, %d lines", len(content), line_count)

    now = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    base, ext = os.path.splitext(file_path)
    target_path = f"{base}_modified_{now}{ext}"
    logger.info("[commit_bicep_chunks] Committing to %s/%s (branch: %s)", repo_name, target_path, branch)

    g = _get_github_client()
    try:
        repo = g.get_repo(repo_name)
        try:
            existing_file = repo.get_contents(target_path, ref=branch)
            logger.info("[commit_bicep_chunks] File exists (sha: %s), updating...", existing_file.sha)
            result = repo.update_file(
                path=target_path,
                message=commit_message,
                content=content,
                sha=existing_file.sha,
                branch=branch,
            )
            logger.info("[commit_bicep_chunks] Updated: %s", result["content"].html_url)
            return f"File updated on GitHub: {result['content'].html_url}"
        except Exception:
            logger.info("[commit_bicep_chunks] Creating new file: %s", target_path)
            result = repo.create_file(
                path=target_path,
                message=commit_message,
                content=content,
                branch=branch,
            )
            logger.info("[commit_bicep_chunks] Created: %s", result["content"].html_url)
            return f"File created on GitHub: {result['content'].html_url}"
    except Exception as e:
        logger.error("[commit_bicep_chunks] Failed: %s", e)
        return f"Error: Failed to commit to {repo_name}/{target_path}: {e}"
    finally:
        g.close()
        logger.info("[commit_bicep_chunks] GitHub connection closed.")
