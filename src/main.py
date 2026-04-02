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
    async for chunk in stream:
        if isinstance(chunk, AgentResponseUpdate) and chunk.text:
            author = chunk.author_name or "Agent"
            if author != last_author:
                step_count += 1
                if last_author is not None:
                    print()
                print(f"\n[{author}]: {chunk.text}", end="", flush=True)
                last_author = author
            else:
                print(chunk.text, end="", flush=True)

    print("\n")

    # Log token usage from the final response
    final_response = await stream.get_final_response()
    logger.info("=" * 70)
    if final_response.usage_details:
        usage = final_response.usage_details
        logger.info(
            "Token usage — input: %s, output: %s, total: %s",
            usage.get("input_token_count", "N/A"),
            usage.get("output_token_count", "N/A"),
            usage.get("total_token_count", "N/A"),
        )
    else:
        logger.info("Token usage: not available")
    logger.info("Total agent steps: %d", step_count)
    logger.info("Orchestration complete.")


if __name__ == "__main__":
    asyncio.run(main())
