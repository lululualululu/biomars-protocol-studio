from biomars.mas.agents.agent_factory import AgentFactory
from biomars.mas.agents.Biologist.ProtocolGeneration.protocol_integrity_validator import ProtocolIntegrityValidator
from biomars.mas.agents.Biologist.ProtocolGeneration.protocol_schemas import ProtocolRequest, DomainPack, ProtocolPlan
from studio.worker import summarize
from pathlib import Path
import yaml


def test_real_interface_constructs_and_finds_packaged_prompts(monkeypatch):
    from biomars.mas.interface import InternAgentInterface
    from biomars.mas.models.model_factory import ModelFactory
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-test-key')
    monkeypatch.setenv('OPENROUTER_BASE_URL', 'https://example.invalid/v1')
    root=Path(__file__).resolve().parents[1]
    config=yaml.safe_load((root/'config/protocol/protocol_config.yaml').read_text())
    AgentFactory._agent_cache.clear()
    ModelFactory._model_cache.clear()
    interface=InternAgentInterface(config=config, task_type='protocol_generation')
    assert set(config['agents']) == set(interface.agents)
    assert (interface.agents['protocol_planning'].prompts_dir/'planner_block_parsing_v1.md').is_file()
    AgentFactory._agent_cache.clear()
    ModelFactory._model_cache.clear()


def test_every_extracted_agent_imports_without_other_workflows():
    names = AgentFactory.get_available_agent_types()
    assert set(names)=={'protocol_domain_pack','protocol_research','protocol_planning','protocol_grounded_writer'}
    for name in names:
        assert AgentFactory._resolve_agent_class(name)


def test_empty_draft_cannot_pass_integrity():
    request = ProtocolRequest(query='Synthetic test', scope='test')
    domain = DomainPack(task=request, lab_profile='', constraints={}, safety_rules={}, env_capabilities={})
    plan = ProtocolPlan(plan_id='empty-test', title='Empty test', task=request, domain_pack=domain)
    result = ProtocolIntegrityValidator().validate(plan, '')
    assert result['approved'] is False
    assert result['issues']


def test_result_does_not_claim_scientific_approval_or_expose_context():
    result=summarize({'state':'completed','context':{'final_protocol_markdown':'# Synthetic draft', 'private_key':'do-not-return', 'protocol_validation_result':{'approved':True,'issues':['A parameter needs review.']},'evidence_bundle':{'items':[{'id':'source-1','title':'Synthetic','url':'https://example.org','markdown':'private full text','meta':{'local_path':'private'}}]}}})
    assert result['state']=='review_required'
    assert 'A parameter needs review.' in result['review_notes']
    assert 'do-not-return' not in str(result)
    assert 'private full text' not in str(result)
    assert 'local_path' not in str(result)


def test_missing_full_text_is_an_actionable_pause():
    result=summarize({'state':'awaiting_feedback','context':{'awaiting_kind':'manual_pdf_upload','manual_pdf_requests':[{'title':'Example','expected_pdf_filename':'source.pdf','internal_path':'secret'}]}})
    assert result['state']=='awaiting_papers'
    assert result['missing_papers'][0]['expected_pdf_filename']=='source.pdf'
    assert 'internal_path' not in str(result)


def test_completed_without_draft_is_not_success():
    assert summarize({'state':'completed','context':{}})['state']=='failed'


def test_public_sources_are_selected_anchors_not_all_candidates():
    result = summarize({'state': 'completed', 'context': {
        'final_protocol_markdown': '# Synthetic draft',
        'primary_anchor_id': 'used',
        'evidence_bundle': {'anchor_ids': ['used'], 'items': [
            {'id': 'used', 'title': 'Used source', 'meta': {'doi': '10.1234/test', 'local_path': 'private'}},
            {'id': 'rejected', 'title': 'Unrelated retrieval candidate'}]}}})
    assert len(result['sources']) == 1
    assert result['sources'][0]['url'] == 'https://doi.org/10.1234/test'
    assert 'private' not in str(result)


def test_completed_resume_clears_resolved_paper_request():
    result = summarize({'state': 'completed', 'context': {
        'final_protocol_markdown': '# Synthetic draft',
        'manual_pdf_requests': [{'title': 'Already uploaded', 'expected_pdf_filename': 'source.pdf'}],
        'protocol_validation_result': {'approved': True, 'issues': [], 'metrics': {'total_steps': 3}}}})
    assert result['missing_papers'] == []
    assert result['integrity_check'] == {'passed': True, 'step_count': 3, 'issue_count': 0}
