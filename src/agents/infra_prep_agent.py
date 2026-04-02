import logging
import os

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential

from tools.bicep_tools import (
    commit_bicep_chunks,
    get_bicep_chunk,
    get_bicep_chunks_batch,
    modify_bicep_file,
    read_bicep_file,
    read_bicep_structure,
    update_bicep_chunk,
    update_bicep_chunks_batch,
)

logger = logging.getLogger(__name__)

INFRA_PREP_INSTRUCTIONS_FULL = """You are an infrastructure preparation agent specializing in Azure Bicep files.

When given a Bicep file path, repository, and branch, you MUST follow these steps in order:

1. **Read** the Bicep file from the remote GitHub repository using the read_bicep_file tool with the repo_name, file_path, and branch.
2. **Analyze and modify** the Bicep content by applying these best-practice improvements:
   - Ensure all resources have a 'tags' property that includes at minimum: Environment, Project, ManagedBy, and LastDeployed.
   - Ensure diagnostic settings are enabled for all supported resources (send logs to Log Analytics).
   - Verify naming conventions follow the Cloud Adoption Framework (CAF) pattern: {resource-type-prefix}-{workload}-{environment}.
   - Add comments summarizing each resource section if missing.
   - Ensure secure parameters use @secure() decorator.
   - Ensure storage accounts have allowBlobPublicAccess set to false and minimumTlsVersion set to TLS1_2.
   - Ensure Key Vaults have enableRbacAuthorization set to true and enablePurgeProtection set to true.
3. **Write** the modified content back to the remote GitHub repo using the modify_bicep_file tool with the repo_name, original file_path, the modified content, branch, and a descriptive commit message. The tool will automatically append _modified_<datetime> to the filename.

After writing, return the GitHub URL of the committed file to the caller.

IMPORTANT:
- Always read the file from GitHub first before making modifications.
- Preserve the overall structure and logic of the Bicep file.
- Only add/modify what is needed — do not remove existing functionality.
- Return the GitHub URL as your final response.
"""

INFRA_PREP_INSTRUCTIONS_CHUNKED = """You are an infrastructure preparation agent specializing in Azure Bicep files.
You operate in CHUNKED mode to minimize token usage by processing individual sections of the file.

When given a Bicep file path, repository, and branch, you MUST follow these steps in order:

1. **Read the file structure** using the read_bicep_structure tool. This returns a summary of all chunks (sections)
   in the file WITHOUT the full content. Review the chunk list to identify which chunks need modification.

2. **For each chunk that needs modification**, do:
   a. Call get_bicep_chunk(index) to retrieve that chunk's content.
   b. Apply ONLY the relevant best-practice improvements to that chunk:
      - Ensure resources have a 'tags' property with at minimum: Environment, Project, ManagedBy, and LastDeployed.
      - Ensure diagnostic settings are enabled for supported resources (send logs to Log Analytics).
      - Verify naming conventions follow the CAF pattern: {resource-type-prefix}-{workload}-{environment}.
      - Add comments summarizing each resource section if missing.
      - Ensure secure parameters use @secure() decorator.
      - Ensure storage accounts have allowBlobPublicAccess set to false and minimumTlsVersion set to TLS1_2.
      - Ensure Key Vaults have enableRbacAuthorization set to true and enablePurgeProtection set to true.
   c. Call update_bicep_chunk(index, modified_content) to store the modification.
   d. Move to the next chunk that needs changes.

3. **Skip chunks** that already comply with best practices — do NOT retrieve or update them.

4. **MANDATORY: Commit the final file.** When ALL chunk modifications are done, you MUST call
   **commit_bicep_chunks** with a descriptive commit message. This is the step that:
   - Reassembles the full file (modified + unmodified chunks)
   - Pushes the final version to the remote GitHub repository
   - Returns the GitHub URL of the committed file
   DO NOT skip this step. The workflow is incomplete without it.

After committing, return the GitHub URL of the committed file to the caller.

IMPORTANT:
- Only retrieve chunks that actually need changes — this saves tokens.
- Preserve the overall structure and logic of the Bicep file.
- Only add/modify what is needed — do not remove existing functionality.
- You MUST call commit_bicep_chunks as your final tool call — otherwise no file is pushed to GitHub.
- Return the GitHub URL as your final response.
"""

INFRA_PREP_INSTRUCTIONS_BATCHED = """You are an infrastructure preparation agent specializing in Azure Bicep files.
You operate in BATCHED mode to minimize token usage by processing multiple sections per round-trip.

When given a Bicep file path, repository, and branch, you MUST follow these steps in order:

1. **Read the file structure** using the read_bicep_structure tool. This returns a summary of all chunks
   WITHOUT the full content. Review the chunk list to identify which chunks need modification.

2. **Group the chunks that need changes into batches of up to 5.** For each batch:
   a. Call get_bicep_chunks_batch with a comma-separated list of indices (e.g. "2,5,7,9,12")
      to retrieve all their contents in one call.
   b. Analyze all returned chunks and prepare the modifications.
   c. Call update_bicep_chunks_batch with a JSON array of {"index": N, "content": "..."} objects
      to store all modifications in one call.
   d. Move to the next batch.

   The best-practice improvements to apply are:
   - Ensure resources have a 'tags' property with at minimum: Environment, Project, ManagedBy, and LastDeployed.
   - Ensure diagnostic settings are enabled for supported resources (send logs to Log Analytics).
   - Verify naming conventions follow the CAF pattern: {resource-type-prefix}-{workload}-{environment}.
   - Add comments summarizing each resource section if missing.
   - Ensure secure parameters use @secure() decorator.
   - Ensure storage accounts have allowBlobPublicAccess set to false and minimumTlsVersion set to TLS1_2.
   - Ensure Key Vaults have enableRbacAuthorization set to true and enablePurgeProtection set to true.

3. **Skip chunks** that already comply with best practices — do NOT retrieve or update them.

4. **MANDATORY: Commit the final file.** When ALL batch modifications are done, you MUST call
   **commit_bicep_chunks** with a descriptive commit message. This:
   - Reassembles the full file (modified + unmodified chunks)
   - Pushes the final version to the remote GitHub repository
   - Returns the GitHub URL of the committed file
   DO NOT skip this step. The workflow is incomplete without it.

After committing, return the GitHub URL of the committed file to the caller.

IMPORTANT:
- Batch retrieval and updates — do NOT process one chunk at a time. Use batches of up to 5.
- Only retrieve chunks that actually need changes — this saves tokens.
- Preserve the overall structure and logic of the Bicep file.
- Only add/modify what is needed — do not remove existing functionality.
- You MUST call commit_bicep_chunks as your final tool call — otherwise no file is pushed to GitHub.
- Return the GitHub URL as your final response.
"""

# ---------------------------------------------------------------------------
# Isolated-batched mode: each sub-task runs as an independent agent.run()
# ---------------------------------------------------------------------------

ISOLATED_ANALYZE_INSTRUCTIONS = """You are an infrastructure analysis agent for Azure Bicep files.

Your ONLY task is to:
1. Call read_bicep_structure to get the chunk summary for the specified file.
2. Review each chunk and determine which ones need best-practice modifications:
   - Resources missing tags (Environment, Project, ManagedBy, LastDeployed).
   - Resources missing diagnostic settings.
   - Naming not following CAF pattern.
   - Missing section comments.
   - Parameters missing @secure() where needed.
   - Storage accounts missing allowBlobPublicAccess/minimumTlsVersion settings.
   - Key Vaults missing enableRbacAuthorization/enablePurgeProtection.
3. Return ONLY a comma-separated list of chunk indices that need modification.
   Example: "2,5,7,9,12,15"

Do NOT retrieve or modify any chunk content. Only analyze the structure summary and return indices.
If no chunks need changes, return "NONE".
"""

ISOLATED_MODIFY_INSTRUCTIONS = """You are an infrastructure modification agent for Azure Bicep files.

Your ONLY task is to:
1. Call get_bicep_chunks_batch to retrieve the specified chunk indices.
2. Apply best-practice improvements to each chunk:
   - Ensure resources have tags: Environment, Project, ManagedBy, LastDeployed.
   - Ensure diagnostic settings are enabled for supported resources.
   - Verify CAF naming: {resource-type-prefix}-{workload}-{environment}.
   - Add section comments if missing.
   - Ensure @secure() on sensitive parameters.
   - Storage: allowBlobPublicAccess=false, minimumTlsVersion=TLS1_2.
   - Key Vault: enableRbacAuthorization=true, enablePurgeProtection=true.
3. Call update_bicep_chunks_batch with a JSON array of {"index": N, "content": "..."} for all modified chunks.
4. Return "BATCH_DONE" when finished.

IMPORTANT:
- Only modify what needs changing — preserve existing structure.
- Do NOT call read_bicep_structure or commit_bicep_chunks.
"""

ISOLATED_COMMIT_INSTRUCTIONS = """You are an infrastructure commit agent.

Your ONLY task is to call commit_bicep_chunks with a descriptive commit message that summarizes the
best-practice improvements applied (tags, diagnostics, naming, security settings, etc.).

Return the GitHub URL from the commit result as your response.
"""


def create_infra_prep_agent(client: FoundryChatClient) -> Agent:
    """Create and return the infra_prep agent with Bicep processing tools.

    Reads BICEP_PROCESSING_MODE from environment:
      - 'full' (default): reads entire file, modifies, writes back in one shot.
      - 'chunked': splits file into sections, processes only chunks that need changes (one at a time).
      - 'batched': splits file into sections, processes chunks in batches of ~5 (fewer round-trips).
      - 'isolated_batched': like batched but each step runs as an independent agent (no history accumulation).
    """
    mode = os.environ.get("BICEP_PROCESSING_MODE", "full").lower()
    logger.info("[create_infra_prep_agent] Processing mode: %s", mode)

    if mode == "isolated_batched":
        # In isolated mode, the main agent is the analyzer — the master_agent orchestrates the rest
        return client.as_agent(
            name="InfraPrepAgent",
            instructions=INFRA_PREP_INSTRUCTIONS_BATCHED,
            tools=[read_bicep_structure, get_bicep_chunks_batch, update_bicep_chunks_batch, commit_bicep_chunks],
        )
    elif mode == "batched":
        return client.as_agent(
            name="InfraPrepAgent",
            instructions=INFRA_PREP_INSTRUCTIONS_BATCHED,
            tools=[read_bicep_structure, get_bicep_chunks_batch, update_bicep_chunks_batch, commit_bicep_chunks],
        )
    elif mode == "chunked":
        return client.as_agent(
            name="InfraPrepAgent",
            instructions=INFRA_PREP_INSTRUCTIONS_CHUNKED,
            tools=[read_bicep_structure, get_bicep_chunk, update_bicep_chunk, commit_bicep_chunks],
        )
    else:
        return client.as_agent(
            name="InfraPrepAgent",
            instructions=INFRA_PREP_INSTRUCTIONS_FULL,
            tools=[read_bicep_file, modify_bicep_file],
        )


def create_isolated_agents(client: FoundryChatClient) -> dict[str, Agent]:
    """Create the three isolated sub-agents for isolated_batched mode.

    Returns a dict with keys: 'analyze', 'modify', 'commit'.
    """
    return {
        "analyze": client.as_agent(
            name="InfraPrepAnalyzer",
            instructions=ISOLATED_ANALYZE_INSTRUCTIONS,
            tools=[read_bicep_structure],
        ),
        "modify": client.as_agent(
            name="InfraPrepModifier",
            instructions=ISOLATED_MODIFY_INSTRUCTIONS,
            tools=[get_bicep_chunks_batch, update_bicep_chunks_batch],
        ),
        "commit": client.as_agent(
            name="InfraPrepCommitter",
            instructions=ISOLATED_COMMIT_INSTRUCTIONS,
            tools=[commit_bicep_chunks],
        ),
    }
