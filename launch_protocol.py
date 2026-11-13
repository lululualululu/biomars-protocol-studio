"""
Launch AutoProtocol
Similar to launch_discovery.py, but for the Protocol Generation Task.
It builds a ProtocolRequest from CLI arguments and launches the MAS session.
"""

import os
import argparse
import asyncio
import logging
import json
from datetime import datetime
from dotenv import load_dotenv


from biomars.mas.interface import InternAgentInterface
from biomars.mas.agents.Biologist.ProtocolGeneration.protocol_schemas import ProtocolRequest

load_dotenv(dotenv_path=os.getenv("DOTENV_PATH", ".env"), override=True)

def setup_logging():
    """Setup logging configuration"""
    log_dir = 'var/logs'
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f'{datetime.now().strftime("%Y%m%d_%H%M%S")}_protocol.log')
    
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(log_file)
        ]
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger("AutoProtocol")

def parse_arguments():
    parser = argparse.ArgumentParser(description="AutoProtocol: Automated Biological Protocol Generation")
    
    # Configuration
    parser.add_argument("--config", type=str, default="config/protocol/protocol_config.yaml", help="Path to config file")
    parser.add_argument(
        "--task_type",
        type=str,
        default="protocol_generation",
        choices=["protocol_generation"],
        help="Task-type routing key used by OrchestrationAgent",
    )
    
    # Request Inputs
    parser.add_argument("--request_json", type=str, help="Path to JSON file containing ProtocolRequest data")
    parser.add_argument("--query", type=str, help="Main protocol query (e.g. 'isolation of primary mouse hepatocytes')")
    parser.add_argument("--title", type=str, help="Protocol title")
    parser.add_argument("--cell_line", type=str, help="Cell line")
    parser.add_argument("--species", type=str, help="Species")
    parser.add_argument("--scope", type=str, default="full_protocol", help="Scope of generation")
    parser.add_argument("--domain_docs", nargs="+", help="List of paths to domain docs (lab profile, SOPs)")
    parser.add_argument("--session_id", type=str, help="Resume an existing session_id (skip creating a new session)")
    parser.add_argument("--manual_pdf_dir", type=str, help="Folder containing manually downloaded PDFs for missing anchors")
    parser.add_argument(
        "--methods_first",
        action="store_true",
        help="Compatibility flag; planning always uses Structure-First.",
    )
    parser.add_argument(
        "--structure_first",
        action="store_true",
        help="Compatibility flag; planning always uses Structure-First.",
    )
    
    return parser.parse_args()

async def run_protocol_pipeline(args):
    logger = logging.getLogger("AutoProtocol")

    logger.info("Protocol planning uses the Structure-First flow.")

    # 1. Initialize Interface
    interface = InternAgentInterface(config_path=args.config, task_name="AutoProtocol")
    await interface.startup()

    session_id = args.session_id
    if not session_id:
        if args.request_json and os.path.exists(args.request_json):
            logger.info(f"Loading request from {args.request_json}")
            with open(args.request_json, "r") as f:
                req_data = json.load(f)
            request = ProtocolRequest(**req_data)
        else:
            if not args.query:
                logger.error("Either --request_json/--query must be provided (or pass --session_id to resume).")
                return

            request = ProtocolRequest(
                query=args.query,
                title=args.title,
                cell_line=args.cell_line,
                species=args.species,
                scope=args.scope,
                user_domain_docs=args.domain_docs or [],
            )

        logger.info(f"Protocol Request: {request.query}")
        session_id = await interface.create_session(
            goal_description=f"Generate protocol for: {request.query}",
            domain="Biology/Protocols",
            task_type=args.task_type,
            payload=request,
        )

    if args.manual_pdf_dir:
        status = await interface.add_feedback(
            session_id=session_id,
            feedback={"manual_pdf_dir": args.manual_pdf_dir},
            auto_resume=True,
        )
    else:
        status = await interface.run_session(session_id)
    
    # 5. Output Result
    # Refresh session data from disk/memory to ensure we have latest context
    # interface.run_session returns status dict, but let's double check via get_session_status
    # or rely on the returned status which comes from session.to_dict()
    
    context = status.get("context", {})
    if status.get("state") == "awaiting_feedback" and (context.get("awaiting_kind") == "manual_pdf_upload"):
        requests = context.get("manual_pdf_requests") or []
        missing = context.get("manual_pdf_missing") or []
        oversized = context.get("manual_pdf_page_limit_failures") or []
        manifest_path = context.get("anchors_manifest_path")
        if manifest_path:
            logger.warning(f"anchors_manifest_path: {manifest_path}")
        if requests:
            logger.warning("Manual PDF upload required for some anchors:")
            for r in requests:
                logger.warning(
                    f"- evidence_id={r.get('evidence_id')} | expected_pdf={r.get('expected_pdf_filename')} | "
                    f"title={r.get('title')} | url={r.get('url')} | reason={r.get('reason')}"
                )
        if missing:
            logger.warning("Missing PDFs in your folder:")
            for m in missing:
                logger.warning(f"- evidence_id={m.get('evidence_id')} | expected_pdf={m.get('expected_pdf_filename')}")
            logger.warning(
                "Download the PDFs, place them into a folder, then rerun:\n"
                f"python launch_protocol.py --session_id {session_id} --manual_pdf_dir /path/to/folder"
            )
        if oversized:
            logger.warning("Oversized PDFs were not parsed:")
            for item in oversized:
                logger.warning(
                    f"- evidence_id={item.get('evidence_id')} | file={item.get('expected_pdf_filename')} | "
                    f"pages={item.get('page_count')} | limit={item.get('max_pages')}"
                )
            logger.warning("Provide the article/chapter PDF rather than the complete book or journal volume.")

    domain_pack = context.get("domain_pack")
    session_file = os.path.join("var", "results", "AutoProtocol", f"traj_{session_id}.json")
    
    # Fallback: check if we can load the session file directly if context is empty
    # (This handles cases where interface might return stale in-memory state if not updated properly)
    if not domain_pack:
        try:
            if os.path.exists(session_file):
                with open(session_file, 'r') as f:
                    session_data = json.load(f)
                    domain_pack = session_data.get("context", {}).get("domain_pack")
        except Exception as e:
            logger.warning(f"Could not load session file backup: {e}")

    if domain_pack:
        output_dir = os.path.join("var", "results", "AutoProtocol")
        os.makedirs(output_dir, exist_ok=True)
        
        # Save DomainPack
        pack_file = os.path.join(output_dir, f"domain_pack_{session_id}.json")
        with open(pack_file, "w") as f:
            if hasattr(domain_pack, "model_dump_json"):
                f.write(domain_pack.model_dump_json(indent=2))
            elif isinstance(domain_pack, dict):
                json.dump(domain_pack, f, indent=2)
            else:
                json.dump(domain_pack, f, default=str, indent=2)

        logger.info(f"DomainPack saved to {pack_file}")
        
        # Print summary
        lab_profile = domain_pack.get("lab_profile", "") if isinstance(domain_pack, dict) else domain_pack.lab_profile
        constraints = domain_pack.get("constraints", {}) if isinstance(domain_pack, dict) else domain_pack.constraints
        safety_rules = domain_pack.get("safety_rules", {}) if isinstance(domain_pack, dict) else domain_pack.safety_rules
        env_capabilities = domain_pack.get("env_capabilities", {}) if isinstance(domain_pack, dict) else domain_pack.env_capabilities
        
        print(f"\n✅ Protocol Domain Pack generated successfully!")
        print(f"   Lab Profile: {len(lab_profile)} chars")
        print(f"   Constraints: {len(constraints)} items")
        print(f"   Safety Rules: {len(safety_rules)} items")
        print(f"   Env Capabilities: {len(env_capabilities)} items")
        print(f"   Output: {pack_file}\n")

    # Check for Protocol Plan
    protocol_plan = context.get("protocol_plan")
    
    # Fallback load from file
    if not protocol_plan and os.path.exists(session_file):
        try:
            with open(session_file, 'r') as f:
                session_data = json.load(f)
                protocol_plan = session_data.get("context", {}).get("protocol_plan")
        except Exception:
            pass

    if protocol_plan:
        plan_dict = protocol_plan if isinstance(protocol_plan, dict) else protocol_plan.dict()
        title = plan_dict.get("title", "Untitled Plan")
        steps = [
            step
            for block in (plan_dict.get("blocks") or [])
            for node in (block.get("timeline_nodes") or [])
            for step in (node.get("steps") or [])
        ]
        if not steps:
            # Historical sessions may still contain only the retired flat view.
            steps = plan_dict.get("steps", [])
        sections = plan_dict.get("sections", [])
        
        print(f"✅ Protocol Plan Generated: {title}")
        print("-" * 60)
        print(f"{'Section':<25} | {'Step Count':<10} | {'Goal'}")
        print("-" * 60)
        for sec in sections:
            s_count = len(sec.get("step_ids", []))
            print(f"{sec.get('name', 'N/A')[:25]:<25} | {s_count:<10} | {sec.get('goal', '')[:40]}...")
        print("-" * 60)
        
        print("\nPreview of First 3 Steps:")
        for step in steps[:3]:
            print(f"  [{step.get('step_id')}] {step.get('phase')}: {step.get('title')}")
        print("...\n")

    if not domain_pack and not protocol_plan:
        logger.error("Session finished but no artifacts (DomainPack or Plan) were produced.")

def main():
    setup_logging()
    args = parse_arguments()
    
    try:
        asyncio.run(run_protocol_pipeline(args))
    except KeyboardInterrupt:
        print("\nInterrupted.")
    except Exception as e:
        logging.getLogger("AutoProtocol").exception(f"Fatal error: {e}")
        import sys
        sys.exit(1)

if __name__ == "__main__":
    main()
