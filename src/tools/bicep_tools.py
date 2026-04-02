import logging
import os
from typing import Annotated

from agent_framework import tool
from github import Auth, Github
from pydantic import Field

logger = logging.getLogger(__name__)

# Resolve the project root (one level up from src/)
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@tool(approval_mode="never_require")
def read_bicep_file(
    file_path: Annotated[str, Field(description="The local filesystem path to the Bicep file to read.")],
) -> str:
    """Read a Bicep file from the local filesystem and return its contents."""
    abs_path = os.path.join(PROJECT_ROOT, file_path) if not os.path.isabs(file_path) else file_path
    logger.info("[read_bicep_file] Resolving path: '%s' -> '%s'", file_path, abs_path)
    if not os.path.exists(abs_path):
        logger.error("[read_bicep_file] File not found: %s", abs_path)
        return f"Error: File not found at {abs_path}"
    with open(abs_path, encoding="utf-8") as f:
        content = f.read()
    line_count = content.count("\n") + 1
    logger.info("[read_bicep_file] Successfully read %d characters (%d lines) from %s", len(content), line_count, abs_path)
    return content


@tool(approval_mode="never_require")
def modify_bicep_file(
    file_path: Annotated[str, Field(description="The local filesystem path to write the modified Bicep content to.")],
    content: Annotated[str, Field(description="The full modified Bicep file content to write.")],
) -> str:
    """Write modified Bicep content back to a local file. Returns the absolute path of the written file."""
    abs_path = os.path.join(PROJECT_ROOT, file_path) if not os.path.isabs(file_path) else file_path
    logger.info("[modify_bicep_file] Resolving path: '%s' -> '%s'", file_path, abs_path)
    dir_path = os.path.dirname(abs_path)
    logger.info("[modify_bicep_file] Ensuring directory exists: %s", dir_path)
    os.makedirs(dir_path, exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
    line_count = content.count("\n") + 1
    logger.info("[modify_bicep_file] Successfully wrote %d characters (%d lines) to %s", len(content), line_count, abs_path)
    return f"Successfully wrote modified Bicep file to {abs_path}"


@tool(approval_mode="never_require")
def commit_to_github(
    file_path: Annotated[str, Field(description="The local filesystem path of the file to commit.")],
    repo_name: Annotated[str, Field(description="The GitHub repository in 'owner/repo' format.")],
    branch: Annotated[str, Field(description="The branch to commit to.")] = "main",
    commit_message: Annotated[str, Field(description="The commit message.")] = "Update Bicep file via infra_prep agent",
    target_path: Annotated[str, Field(description="The path in the repository where the file should be placed (e.g. 'infra/main.bicep').")] = "",
) -> str:
    """Read a local file and commit it to a GitHub repository. Returns the GitHub URL of the committed file."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        logger.error("[commit_to_github] GITHUB_TOKEN environment variable is not set.")
        return "Error: GITHUB_TOKEN environment variable is not set."

    abs_path = os.path.join(PROJECT_ROOT, file_path) if not os.path.isabs(file_path) else file_path
    logger.info("[commit_to_github] Resolving path: '%s' -> '%s'", file_path, abs_path)
    if not os.path.exists(abs_path):
        logger.error("[commit_to_github] File not found: %s", abs_path)
        return f"Error: File not found at {abs_path}"
    with open(abs_path, encoding="utf-8") as f:
        content = f.read()
    logger.info("[commit_to_github] Read %d characters from %s", len(content), abs_path)

    repo_path = target_path if target_path else os.path.relpath(abs_path).replace("\\", "/")
    logger.info("[commit_to_github] Target repo path: %s", repo_path)

    auth = Auth.Token(token)
    g = Github(auth=auth)
    try:
        logger.info("[commit_to_github] Connecting to GitHub repo: %s", repo_name)
        repo = g.get_repo(repo_name)
        logger.info("[commit_to_github] Committing to %s/%s on branch '%s' with message: '%s'", repo_name, repo_path, branch, commit_message)

        try:
            existing_file = repo.get_contents(repo_path, ref=branch)
            logger.info("[commit_to_github] File already exists (sha: %s), updating...", existing_file.sha)
            result = repo.update_file(
                path=repo_path,
                message=commit_message,
                content=content,
                sha=existing_file.sha,
                branch=branch,
            )
            logger.info("[commit_to_github] Successfully updated file: %s", result["content"].html_url)
            return f"File updated on GitHub: {result['content'].html_url}"
        except Exception as e:
            logger.info("[commit_to_github] File does not exist yet (%s), creating new file...", e)
            result = repo.create_file(
                path=repo_path,
                message=commit_message,
                content=content,
                branch=branch,
            )
            logger.info("[commit_to_github] Successfully created new file: %s", result["content"].html_url)
            return f"File created on GitHub: {result['content'].html_url}"
    finally:
        g.close()
        logger.info("[commit_to_github] GitHub connection closed.")
