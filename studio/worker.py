"""Run one real pipeline in its own process and working directory."""
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

import yaml

from studio.storage import write_json

ROOT = Path(__file__).resolve().parents[1]


def summarize(status):
    context = status.get('context', {})
    markdown = context.get('final_protocol_markdown', '') or ''
    bundle = context.get('evidence_bundle') or {}
    if hasattr(bundle, 'model_dump'):
        bundle = bundle.model_dump()
    items = bundle.get('items', bundle.get('evidence', [])) if isinstance(bundle, dict) else []
    sources = [{key: item.get(key) for key in ('id', 'evidence_id', 'title', 'url', 'doi', 'year', 'source')} for item in items if isinstance(item, dict)]
    missing = [{key: item.get(key) for key in ('evidence_id', 'title', 'url', 'expected_pdf_filename', 'reason')} for item in context.get('manual_pdf_requests', []) if isinstance(item, dict)]
    validation = context.get('protocol_validation_result') or {}
    notes = [str(issue) for issue in validation.get('issues', []) if isinstance(issue, str)]
    notes.append('Review the draft, source applicability, and unresolved parameters before use.')
    state = status.get('state', 'failed')
    if state == 'awaiting_feedback':
        state = 'awaiting_papers' if context.get('awaiting_kind') == 'manual_pdf_upload' else 'review_required'
    elif state == 'completed':
        state = 'review_required' if markdown else 'failed'
    elif state not in ('failed', 'cancelled'):
        state = 'interrupted'
    return {'state': state, 'stage': status.get('state'), 'markdown': markdown,
            'sources': sources, 'missing_papers': missing,
            'review_notes': notes,
            'message': 'The run stopped before a draft was completed. Check the private server log.' if state in ('failed', 'interrupted') else ''}


async def run(directory):
    os.chdir(directory)
    from biomars.mas.interface import InternAgentInterface
    from biomars.mas.agents.Biologist.ProtocolGeneration.protocol_schemas import ProtocolRequest

    request = json.loads(Path('request.json').read_text())
    config = yaml.safe_load((ROOT / 'config/protocol/protocol_config.yaml').read_text())
    if os.getenv('OPENROUTER_MODEL'):
        config['models']['openrouter']['model_name'] = os.environ['OPENROUTER_MODEL']
    config['memory']['file_dir'] = str(Path('var/results').resolve())
    interface = InternAgentInterface(config=config, task_name='AutoProtocol', task_type='protocol_generation')
    required = set(config['agents'])
    if not required.issubset(interface.agents):
        raise RuntimeError('Required generation agents could not be initialized')
    await interface.startup()
    session_file = Path('session.json')

    async def progress(session_id, old_state, new_state):
        write_json('status.json', {'state': 'running', 'stage': new_state})

    try:
        if session_file.exists():
            session_id = json.loads(session_file.read_text())['id']
            # Add feedback without running first, so resumed work uses the same progress callback.
            await interface.add_feedback(session_id, {'manual_pdf_dir': str(Path('papers').resolve())}, auto_resume=False)
        else:
            payload = ProtocolRequest(**request)
            session_id = await interface.create_session(goal_description=request['query'], domain='Biology/Protocols', task_type='protocol_generation', payload=payload)
            write_json(session_file, {'id': session_id})
        status = await interface.run_session(session_id, status_callback=progress)
        result = summarize(status)
        if result['markdown']:
            Path('protocol.md').write_text(result['markdown'], encoding='utf-8')
        write_json('status.json', result)
    finally:
        await interface.shutdown()


if __name__ == '__main__':
    directory = Path(sys.argv[1]).resolve()
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    try:
        asyncio.run(run(directory))
    except Exception:
        logging.exception('Generation failed')
        write_json(directory / 'status.json', {'state': 'failed', 'message': 'Generation failed. The server operator can inspect the private job log.'})
        sys.exit(1)
