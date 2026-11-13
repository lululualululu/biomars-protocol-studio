import json
from pathlib import Path

import fitz
import pytest
from fastapi.testclient import TestClient

from studio.api import create_app
from studio.storage import write_json


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.setenv('OPENROUTER_API_KEY', 'synthetic-test-credential')
    monkeypatch.setenv('STUDIO_ACCESS_TOKEN', 'test-access-code')
    return create_app(tmp_path, run_queue=False)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        yield client


def create(client, query='Plan a cell culture literature review.', **kwargs):
    return client.post('/api/jobs', data={'payload': json.dumps({'query': query})}, headers={'Authorization': 'Bearer test-access-code'}, **kwargs)


def pdf_bytes(text='Synthetic source document for upload testing.'):
    with fitz.open() as doc:
        page = doc.new_page()
        page.insert_text((72, 72), text)
        return doc.tobytes()


def test_static_entry_and_config(client):
    assert client.get('/').status_code == 200
    response = client.get('/api/config')
    assert response.json()['generation_available'] is True
    assert 'synthetic-test-credential' not in response.text
    assert 'no-store' in response.headers['cache-control']


def test_generation_needs_config(client, monkeypatch):
    monkeypatch.delenv('OPENROUTER_API_KEY')
    assert create(client).status_code == 503


def test_generation_requires_access_code(client):
    response = client.post('/api/jobs', data={'payload': json.dumps({'query': 'A valid synthetic test request'})})
    assert response.status_code == 401


def test_task_ownership_covers_read_export_resume_and_delete(client, app):
    response = create(client)
    assert response.status_code == 202
    job_id = response.json()['id']
    assert 'owner' not in response.json()
    directory = app.state.store.root / job_id
    (directory / 'protocol.md').write_text('# Synthetic draft')
    with TestClient(app) as stranger:
        assert stranger.get('/api/jobs').json() == []
        assert stranger.get(f'/api/jobs/{job_id}').status_code == 404
        assert stranger.get(f'/api/jobs/{job_id}/export').status_code == 404
        assert stranger.delete(f'/api/jobs/{job_id}').status_code == 404
        assert stranger.post(f'/api/jobs/{job_id}/resume', files={'files': ('paper.pdf',pdf_bytes())}, headers={'Authorization':'Bearer test-access-code'}).status_code == 404
    assert client.get(f'/api/jobs/{job_id}').status_code == 200


def test_reference_path_is_generated_and_text_extracted(client, app):
    response = create(client, files={'files': ('../../private.pdf', pdf_bytes(), 'application/pdf')})
    assert response.status_code == 202
    directory = app.state.store.root / response.json()['id']
    payload = json.loads((directory / 'request.json').read_text())
    path = Path(payload['user_domain_docs'][0])
    assert path.parent == directory / 'references'
    assert 'Synthetic source' in path.read_text()


@pytest.mark.parametrize('name,content', [('payload.exe', b'test'), ('paper.pdf', b'not a PDF'), ('empty.txt', b''), ('binary.md', b'\xff\xfe')])
def test_invalid_upload_is_rejected_and_cleaned(client, app, name, content):
    assert create(client, files={'files': (name,content)}).status_code in (413,422)
    assert not list(app.state.store.root.glob('*/request.json'))
    assert client.get('/api/jobs').json() == []


def test_oversized_file(client):
    assert create(client, files={'files': ('large.txt', b'x'*(10*1024*1024+1))}).status_code == 413


def test_short_query_or_arbitrary_server_path_rejected(client):
    assert create(client, query='short').status_code == 422
    response = client.post('/api/jobs', data={'payload': json.dumps({'query':'A valid synthetic test request', 'user_domain_docs':['/etc/passwd']})}, headers={'Authorization':'Bearer test-access-code'})
    assert response.status_code == 422


def test_cross_origin_mutation_rejected(client):
    assert client.post('/api/jobs',headers={'Origin':'https://unrelated.example'}).status_code == 403


def test_bounded_queue(client, monkeypatch):
    monkeypatch.setenv('STUDIO_MAX_PENDING','1')
    assert create(client).status_code == 202
    assert create(client).status_code == 429


def test_missing_paper_resume_keeps_session(client, app):
    job_id=create(client).json()['id']
    root=app.state.store.root/job_id
    write_json(root/'session.json',{'id':'original-session'})
    app.state.store.update(job_id,'awaiting_papers')
    write_json(root/'status.json',{'state':'awaiting_papers','missing_papers':[{'expected_pdf_filename':'anchor.pdf','title':'Synthetic paper'}]})
    headers={'Authorization':'Bearer test-access-code'}
    assert client.post(f'/api/jobs/{job_id}/resume', files={'files':('wrong.pdf',pdf_bytes())},headers=headers).status_code==422
    response=client.post(f'/api/jobs/{job_id}/resume', files={'files':('anchor.pdf',pdf_bytes())},headers=headers)
    assert response.status_code==202
    assert response.json()['id']==job_id
    assert json.loads((root/'session.json').read_text())['id']=='original-session'
    assert (root/'papers/anchor.pdf').exists()


def test_cancel_then_delete_removes_files(client, app):
    job_id=create(client).json()['id']
    assert client.delete(f'/api/jobs/{job_id}').status_code==409
    assert client.post(f'/api/jobs/{job_id}/cancel').json()['state']=='cancelled'
    assert client.delete(f'/api/jobs/{job_id}').status_code==204
    assert not (app.state.store.root/job_id).exists()


def test_restart_marks_running_as_interrupted(app):
    app.state.store.create('synthetic-job','owner','Synthetic')
    app.state.store.update('synthetic-job','running')
    with TestClient(app):
        assert app.state.store.get('synthetic-job')['state']=='interrupted'


def test_private_paths_never_served(client):
    assert client.get('/.env').status_code==404
    assert client.get('/var/studio/jobs.sqlite3').status_code==404
    assert client.get('/studio/api.py').status_code==404


def test_streaming_body_limit_cannot_be_bypassed(client, monkeypatch):
    import studio.api
    monkeypatch.setattr(studio.api, 'MAX_BODY', 300)
    def chunks():
        yield b'payload=' + b'x'*301
    result=client.post('/api/jobs',content=chunks(),headers={'Content-Type':'application/x-www-form-urlencoded','Authorization':'Bearer test-access-code'})
    assert result.status_code==413
