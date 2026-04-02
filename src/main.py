import asyncio
import logging
import os
import sys

from agent_framework import AgentResponseUpdate
from agent_framework.foundry import FoundryChatClient
from azure.identity import AzureCliCredential
from dotenv import load_dotenv

from agents.infra_prep_agent import create_infra_prep_agent
from agents.master_agent import create_master_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)


async def main() -> None:
    load_dotenv()

    project_endpoint = os.environ.get("AZURE_AI_PROJECT_ENDPOINT")
    model = os.environ.get("AZURE_AI_MODEL_DEPLOYMENT_NAME")

    if not project_endpoint or not model:
        logger.error(
            "Missing required environment variables: AZURE_AI_PROJECT_ENDPOINT and/or AZURE_AI_MODEL_DEPLOYMENT_NAME. "
            "Copy .env.example to .env and fill in your values."
        )
        sys.exit(1)

    logger.info("Initializing FoundryChatClient...")
    logger.info("  Project endpoint: %s", project_endpoint)
    logger.info("  Model: %s", model)

    client = FoundryChatClient(
        project_endpoint=project_endpoint,
        model=model,
        credential=AzureCliCredential(),
    )

    # Create the infra_prep agent (with Bicep tools)
    logger.info("Creating InfraPrepAgent...")
    infra_prep_agent = create_infra_prep_agent(client)

    # Create the master orchestrator agent (with infra_prep as a tool)
    logger.info("Creating MasterOrchestratorAgent...")
    master_agent = create_master_agent(client, infra_prep_agent)

    # Run the master agent with a sample prompt
    user_prompt = (
        "Process the Bicep file at infra/main.bicep and commit the improved version to GitHub."
    )
    logger.info("=" * 70)
    logger.info("User prompt: %s", user_prompt)
    logger.info("=" * 70)

    # Stream the response to show real-time orchestration
    last_author = None
    print("\n")

    stream = master_agent.run(user_prompt, stream=True)
    step_count = 0
    chunk_count = 0
    chars_by_author: dict[str, int] = {}

    async for chunk in stream:
        if isinstance(chunk, AgentResponseUpdate):
            chunk_count += 1
            author = chunk.author_name or "Agent"

            if chunk.text:
                chars_by_author[author] = chars_by_author.get(author, 0) + len(chunk.text)

                if author != last_author:
                    step_count += 1
                    if last_author is not None:
                        print()
                        logger.info("[streaming] Step %d complete — author: %s, chars so far: %d",
                                    step_count - 1, last_author, chars_by_author.get(last_author, 0))
                    logger.info("[streaming] Step %d started — author: %s", step_count, author)
                    print(f"\n[{author}]: {chunk.text}", end="", flush=True)
                    last_author = author
                else:
                    print(chunk.text, end="", flush=True)

            # Log additional_properties if they contain usage info
            if chunk.additional_properties:
                usage_props = {k: v for k, v in chunk.additional_properties.items() if "token" in k.lower() or "usage" in k.lower()}
                if usage_props:
                    logger.info("[streaming] Chunk %d additional usage properties: %s", chunk_count, usage_props)

    print("\n")
    logger.info("[streaming] Stream complete — total chunks: %d, total steps: %d", chunk_count, step_count)
    for author, chars in chars_by_author.items():
        logger.info("[streaming] Author '%s' produced %d characters", author, chars)

    # Log token usage from the final response
    final_response = await stream.get_final_response()
    logger.info("=" * 70)
    logger.info("FINAL TOKEN USAGE SUMMARY")
    logger.info("=" * 70)
    if final_response.usage_details:
        usage = final_response.usage_details
        logger.info(
            "[MasterAgent] Token usage — input: %s, output: %s, total: %s",
            usage.get("input_token_count", "N/A"),
            usage.get("output_token_count", "N/A"),
            usage.get("total_token_count", "N/A"),
        )
        # Log any extra usage details (e.g. cached tokens, reasoning tokens)
        extra_keys = [k for k in usage if k not in ("input_token_count", "output_token_count", "total_token_count")]
        if extra_keys:
            for k in extra_keys:
                logger.info("[MasterAgent] Token usage detail — %s: %s", k, usage[k])
    else:
        logger.info("[MasterAgent] Token usage: not available")
    logger.info("Total agent steps: %d", step_count)
    logger.info("Orchestration complete.")


if __name__ == "__main__":
    asyncio.run(main())
