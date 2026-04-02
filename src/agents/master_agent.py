import logging
import os
from typing import Annotated

from agent_framework import Agent, tool
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


def create_master_agent(client: FoundryChatClient, infra_prep_agent: Agent) -> Agent:
    """Create the master orchestrator agent that delegates to infra_prep."""

    @tool(approval_mode="never_require")
    async def process_bicep_with_infra_prep(
        file_path: Annotated[str, Field(description="The local filesystem path to the Bicep file to process.")],
        repo_name: Annotated[str, Field(description="The GitHub repository in 'owner/repo' format.")] = "",
        branch: Annotated[str, Field(description="The GitHub branch to commit to.")] = "",
    ) -> str:
        """Delegate Bicep file processing to the infra_prep agent. It will read the file, apply best-practice
        modifications, commit the result to GitHub, and return the GitHub URL."""
        repo = repo_name or os.environ.get("GITHUB_REPO", "")
        br = branch or os.environ.get("GITHUB_BRANCH", "main")

        prompt = (
            f"Process the Bicep file at '{file_path}'. "
            f"After modifying it, commit it to GitHub repository '{repo}' on branch '{br}'. "
            f"Return the GitHub URL of the committed file."
        )

        result = await infra_prep_agent.run(prompt)

        # Log token usage from the inner infra_prep agent run
        if result.usage_details:
            usage = result.usage_details
            logger.info(
                "[InfraPrepAgent] Token usage — input: %s, output: %s, total: %s",
                usage.get("input_token_count", "N/A"),
                usage.get("output_token_count", "N/A"),
                usage.get("total_token_count", "N/A"),
            )
            # Log any additional usage fields (e.g. cache, reasoning tokens)
            extra_keys = [k for k in usage if k not in ("input_token_count", "output_token_count", "total_token_count")]
            if extra_keys:
                for k in extra_keys:
                    logger.info("[InfraPrepAgent] Token usage detail — %s: %s", k, usage[k])
        else:
            logger.info("[InfraPrepAgent] Token usage: not available")

        return result.text

    return client.as_agent(
        name="MasterOrchestratorAgent",
        instructions=MASTER_AGENT_INSTRUCTIONS,
        tools=[process_bicep_with_infra_prep],
    )
