import logging
import os
from typing import Annotated

from agent_framework import Agent, UsageDetails, add_usage_details, tool
from agent_framework.foundry import FoundryChatClient
from pydantic import Field

logger = logging.getLogger(__name__)

MASTER_AGENT_INSTRUCTIONS = """You are a master orchestration agent for infrastructure-as-code workflows.

Your role is to:
1. Parse the user's request to identify the Bicep file path they want processed.
2. Determine the GitHub repository and branch where the result should be committed.
   - Default repository: use the GITHUB_REPO environment variable.
   - Default branch: use the GITHUB_BRANCH environment variable, or 'main' if not set.
3. Delegate the actual infrastructure preparation work to the infra_prep agent by calling the
   process_bicep_with_infra_prep tool with the extracted file path, repo, and branch.
4. Report the result back to the user, including the GitHub URL of the committed file.

When responding:
- Confirm which file you identified from the user's request.
- Show the GitHub URL returned by the infra_prep agent.
- Provide a brief summary of what was done.

If the user doesn't specify a file path, ask them for one.
"""


def _log_usage(label: str, usage: UsageDetails | None) -> None:
    """Log token usage from an agent run."""
    if usage:
        logger.info(
            "[%s] Token usage — input: %s, output: %s, total: %s",
            label,
            usage.get("input_token_count", "N/A"),
            usage.get("output_token_count", "N/A"),
            usage.get("total_token_count", "N/A"),
        )
        extra_keys = [k for k in usage if k not in ("input_token_count", "output_token_count", "total_token_count")]
        for k in extra_keys:
            logger.info("[%s] Token usage detail — %s: %s", label, k, usage[k])
    else:
        logger.info("[%s] Token usage: not available", label)


def create_master_agent(
    client: FoundryChatClient,
    infra_prep_agent: Agent,
    isolated_agents: dict[str, Agent] | None = None,
) -> Agent:
    """Create the master orchestrator agent that delegates to infra_prep.

    Args:
        client: The FoundryChatClient.
        infra_prep_agent: The standard infra_prep agent (used in full/chunked/batched modes).
        isolated_agents: Optional dict of {'analyze', 'modify', 'commit'} agents for isolated_batched mode.
    """
    mode = os.environ.get("BICEP_PROCESSING_MODE", "full").lower()

    @tool(approval_mode="never_require")
    async def process_bicep_with_infra_prep(
        file_path: Annotated[str, Field(description="The path to the Bicep file in the repository (e.g. 'infra/main.bicep').")],
        repo_name: Annotated[str, Field(description="The GitHub repository in 'owner/repo' format.")] = "",
        branch: Annotated[str, Field(description="The GitHub branch to commit to.")] = "",
    ) -> str:
        """Delegate Bicep file processing to the infra_prep agent. It will read the file, apply best-practice
        modifications, commit the result to GitHub, and return the GitHub URL."""
        repo = repo_name or os.environ.get("GITHUB_REPO", "")
        br = branch or os.environ.get("GITHUB_BRANCH", "main")

        if mode == "isolated_batched" and isolated_agents:
            return await _run_isolated_batched(isolated_agents, file_path, repo, br)

        prompt = (
            f"Process the Bicep file at '{file_path}'. "
            f"After modifying it, commit it to GitHub repository '{repo}' on branch '{br}'. "
            f"Return the GitHub URL of the committed file."
        )

        result = await infra_prep_agent.run(prompt)
        _log_usage("InfraPrepAgent", result.usage_details)
        return result.text

    return client.as_agent(
        name="MasterOrchestratorAgent",
        instructions=MASTER_AGENT_INSTRUCTIONS,
        tools=[process_bicep_with_infra_prep],
    )


async def _run_isolated_batched(
    agents: dict[str, Agent],
    file_path: str,
    repo: str,
    branch: str,
) -> str:
    """Run the isolated batched workflow — each step is an independent agent.run() with no shared history."""
    total_usage: UsageDetails | None = None

    # --- Step 1: Analyze (fresh agent run) ---
    logger.info("[isolated_batched] Step 1: Analyzing structure of %s/%s (branch: %s)", repo, file_path, branch)
    analyze_prompt = (
        f"Read the structure of the Bicep file at path '{file_path}' in repo '{repo}' on branch '{branch}'. "
        f"Return ONLY a comma-separated list of chunk indices that need best-practice modifications."
    )
    analyze_result = await agents["analyze"].run(analyze_prompt)
    _log_usage("isolated/analyze", analyze_result.usage_details)
    total_usage = add_usage_details(total_usage, analyze_result.usage_details)

    indices_text = analyze_result.text.strip()
    logger.info("[isolated_batched] Analyzer returned indices: %s", indices_text)

    if indices_text.upper() == "NONE" or not indices_text:
        return "No chunks need modification — file already complies with best practices."

    # Parse indices and split into batches of 5
    try:
        all_indices = [int(i.strip()) for i in indices_text.replace("\n", ",").split(",") if i.strip().isdigit()]
    except ValueError:
        return f"Error: Analyzer returned invalid indices: {indices_text}"

    if not all_indices:
        return "No chunks need modification — file already complies with best practices."

    batches = [all_indices[i:i + 5] for i in range(0, len(all_indices), 5)]
    logger.info("[isolated_batched] %d chunks to modify in %d batches: %s", len(all_indices), len(batches), batches)

    # --- Step 2: Modify each batch (fresh agent run per batch) ---
    for batch_num, batch in enumerate(batches, 1):
        indices_str = ",".join(str(i) for i in batch)
        logger.info("[isolated_batched] Step 2.%d: Modifying batch %s", batch_num, indices_str)

        modify_prompt = (
            f"Retrieve chunks {indices_str} using get_bicep_chunks_batch, "
            f"apply best-practice improvements, "
            f"then store the modified versions using update_bicep_chunks_batch. "
            f"Return 'BATCH_DONE' when finished."
        )
        modify_result = await agents["modify"].run(modify_prompt)
        _log_usage(f"isolated/modify-batch-{batch_num}", modify_result.usage_details)
        total_usage = add_usage_details(total_usage, modify_result.usage_details)
        logger.info("[isolated_batched] Batch %d result: %s", batch_num, modify_result.text[:100])

    # --- Step 3: Commit (fresh agent run) ---
    logger.info("[isolated_batched] Step 3: Committing to GitHub")
    commit_prompt = (
        "Call commit_bicep_chunks with a commit message describing the best-practice improvements applied. "
        "Return the GitHub URL."
    )
    commit_result = await agents["commit"].run(commit_prompt)
    _log_usage("isolated/commit", commit_result.usage_details)
    total_usage = add_usage_details(total_usage, commit_result.usage_details)

    logger.info("[isolated_batched] === ISOLATED BATCHED TOTAL USAGE ===")
    _log_usage("isolated/TOTAL", total_usage)

    return commit_result.text
