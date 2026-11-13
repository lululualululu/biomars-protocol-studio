"""Single-instance, invitation-only API for the extracted Python engine."""
import asyncio
import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import sys
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from studio.storage import Store, write_json

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / '.env', override=False)
MAX_FILE = 10 * 1024 * 1024
MAX_BODY = 51 * 1024 * 1024


class BodyLimit:
    """Bound streaming requests too, before multipart parsing allocates storage."""
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        received = 0
        over_limit = False

        async def bounded_receive():
            nonlocal received, over_limit
            message = await receive()
            received += len(message.get('body', b''))
            if received > MAX_BODY:
                over_limit = True
                raise HTTPException(413, 'Upload is too large.')
            return message

        async def bounded_send(message):
            # FastAPI can translate multipart parsing errors to 400; preserve the
            # size-specific status without allowing parsing to continue.
            if over_limit and message['type'] == 'http.response.start':
                message = dict(message, status=413)
            await send(message)

        await self.app(scope, bounded_receive, bounded_send)


class GenerationRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    query: str = Field(min_length=12, max_length=6000)
    cell_line: str | None = Field(default=None, max_length=120)
    species: str | None = Field(default=None, max_length=120)
    scope: str = Field(default='full_protocol', max_length=500)


def terminate(process):
    if process and process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)


class Queue:
    def __init__(self, store):
        self.store = store
        self.process = None
        self.active = None
        self.enqueue_lock = asyncio.Lock()

    async def loop(self):
        while True:
            job = self.store.next()
            if not job:
                await asyncio.sleep(.4)
                continue
            self.active = job['id']
            directory = self.store.root / self.active
            self.store.update(self.active, 'running')
            write_json(directory / 'status.json', {'state': 'running', 'stage': 'initial'})
            try:
                env = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONUNBUFFERED='1')
                with (directory / 'private.log').open('ab') as log:
                    self.process = await asyncio.create_subprocess_exec(sys.executable, '-m', 'studio.worker', str(directory), cwd=ROOT, env=env, stdout=log, stderr=log, start_new_session=True)
                    await asyncio.wait_for(self.process.wait(), timeout=int(os.getenv('STUDIO_JOB_TIMEOUT', '1800')))
                state = json.loads((directory / 'status.json').read_text()).get('state', 'failed')
                if state == 'running':
                    state = 'failed'
                if self.store.get(self.active)['state'] != 'cancelled':
                    self.store.update(self.active, state)
            except asyncio.CancelledError:
                terminate(self.process)
                if self.process:
                    await self.process.wait()
                self.store.update(self.active, 'interrupted')
                raise
            except Exception:
                terminate(self.process)
                if self.process:
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.process.wait(), timeout=5)
                    if self.process.returncode is None:
                        os.killpg(self.process.pid, signal.SIGKILL)
                        await self.process.wait()
                if self.store.get(self.active)['state'] != 'cancelled':
                    self.store.update(self.active, 'failed')
                    write_json(directory / 'status.json', {'state': 'failed', 'message': 'The task timed out or stopped. Check service configuration and try again.'})
            finally:
                self.process = None
                self.active = None


def create_app(data_dir=None, run_queue=True):
    store = Store(data_dir or os.getenv('STUDIO_DATA_DIR', str(ROOT / 'var/studio')))
    queue = Queue(store)

    @asynccontextmanager
    async def lifespan(app):
        store.recover()
        task = asyncio.create_task(queue.loop()) if run_queue else None
        yield
        if task:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title='BioMars Protocol Studio', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.store = store
    app.add_middleware(BodyLimit)

    @app.middleware('http')
    async def browser_boundary(request, call_next):
        if request.method not in ('GET', 'HEAD', 'OPTIONS'):
            origin = request.headers.get('origin')
            if origin and (urlsplit(origin).scheme not in ('http', 'https') or urlsplit(origin).netloc != request.headers.get('host')):
                return JSONResponse({'detail': 'Cross-origin requests are not allowed.'}, status_code=403)
            try:
                size = int(request.headers.get('content-length', '0'))
            except ValueError:
                return JSONResponse({'detail': 'Invalid content length.'}, status_code=400)
            if size > MAX_BODY:
                return JSONResponse({'detail': 'Upload is too large.'}, status_code=413)
            if request.method == 'POST' and (request.url.path == '/api/jobs' or request.url.path.endswith('/resume')):
                try:
                    authorize(request)
                except HTTPException as exc:
                    return JSONResponse({'detail': exc.detail}, status_code=exc.status_code)
        cookie = request.cookies.get('studio_session', '')
        fresh = not re.fullmatch(r'[A-Za-z0-9_-]{43}', cookie)
        if fresh:
            cookie = secrets.token_urlsafe(32)
        request.state.owner = hashlib.sha256(cookie.encode()).hexdigest()
        response = await call_next(request)
        if fresh:
            response.set_cookie('studio_session', cookie, max_age=60*60*24*30, httponly=True, samesite='strict', secure=os.getenv('STUDIO_SECURE_COOKIES') == '1')
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'same-origin'
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'no-store'
        return response

    def authorize(request):
        token = os.getenv('STUDIO_ACCESS_TOKEN', '')
        if not os.getenv('OPENROUTER_API_KEY') or not token:
            raise HTTPException(503, 'Real generation is not configured. Explore the example, or configure the Python service.')
        supplied = request.headers.get('authorization', '').removeprefix('Bearer ')
        if not secrets.compare_digest(supplied, token):
            raise HTTPException(401, 'Enter the service access code in settings to generate.')

    def owned(job_id, request):
        job = store.get(job_id, request.state.owner)
        if not job:
            raise HTTPException(404, 'Task not found.')
        return job

    def public(job, detail=False):
        result = {k: job[k] for k in ('id', 'title', 'state', 'created', 'updated')}
        if detail:
            path = store.root / job['id'] / 'status.json'
            if path.exists():
                with contextlib.suppress(ValueError):
                    status = json.loads(path.read_text())
                    result.update({k: v for k, v in status.items() if k in ('stage', 'markdown', 'sources', 'missing_papers', 'review_notes', 'message')})
        return result

    async def save_files(files, directory, papers=False, expected=None):
        if len(files) > 5:
            raise HTTPException(422, 'Attach up to 5 files.')
        directory.mkdir(parents=True, exist_ok=True)
        paths = []
        for index, upload in enumerate(files):
            name = Path(upload.filename or 'document').name
            suffix = Path(name).suffix.lower()
            if suffix not in (('.pdf',) if papers else ('.pdf', '.md', '.txt')):
                raise HTTPException(422, 'Supported files: PDF, Markdown, plain text.')
            content = await upload.read(MAX_FILE + 1)
            await upload.close()
            if not content or len(content) > MAX_FILE:
                raise HTTPException(413, 'Each attachment must be non-empty and at most 10 MB.')
            if suffix == '.pdf':
                if not content.startswith(b'%PDF-'):
                    raise HTTPException(422, 'The file is not a PDF.')
                import fitz
                try:
                    with fitz.open(stream=content, filetype='pdf') as pdf:
                        if pdf.is_encrypted or pdf.page_count > 30:
                            raise ValueError('Use an unencrypted article of up to 30 pages.')
                        text = '\n\n'.join(page.get_text() for page in pdf)
                except Exception as exc:
                    raise HTTPException(422, 'Use a readable, unencrypted PDF of up to 30 pages.') from exc
            else:
                try:
                    text = content.decode('utf-8')
                except UnicodeDecodeError as exc:
                    raise HTTPException(422, 'Text attachments must use UTF-8.') from exc
            if papers:
                if name not in (expected or set()):
                    raise HTTPException(422, 'Rename the PDF to the requested filename before uploading.')
                path = directory / name
                path.write_bytes(content)
            else:
                if not text.strip():
                    raise HTTPException(422, 'This PDF has no readable text. Upload a text-native PDF or Markdown.')
                path = directory / f'reference-{index+1}.txt'
                path.write_text(text[:500000], encoding='utf-8')
            paths.append(str(path.resolve()))
        return paths

    @app.get('/api/config')
    async def config():
        return {'name': 'BioMars Protocol Studio', 'generation_available': bool(os.getenv('OPENROUTER_API_KEY') and os.getenv('STUDIO_ACCESS_TOKEN')), 'access_code_required': True, 'max_file_mb': 10}

    @app.get('/api/jobs')
    async def jobs(request: Request):
        return [public(j) for j in store.list(request.state.owner)]

    @app.post('/api/jobs', status_code=202)
    async def create(request: Request, payload: str = Form(...), files: list[UploadFile] = File(default=[])):
        authorize(request)
        try:
            data = GenerationRequest.model_validate_json(payload).model_dump()
        except ValidationError:
            raise HTTPException(422, 'Describe your experiment in 12–6000 characters; use the optional fields for context.')
        job_id = uuid.uuid4().hex
        directory = store.root / job_id
        async with queue.enqueue_lock:
            if store.pending() >= int(os.getenv('STUDIO_MAX_PENDING', '5')):
                raise HTTPException(429, 'The generation queue is full. Please try again later.')
            try:
                paths = await save_files(files, directory / 'references')
                data['user_domain_docs'] = paths
                write_json(directory / 'request.json', data)
                store.create(job_id, request.state.owner, data['query'][:120])
            except Exception:
                shutil.rmtree(directory, ignore_errors=True)
                raise
        return public(store.get(job_id))

    @app.get('/api/jobs/{job_id}')
    async def get_job(job_id: str, request: Request):
        return public(owned(job_id, request), True)

    @app.post('/api/jobs/{job_id}/resume', status_code=202)
    async def resume(job_id: str, request: Request, files: list[UploadFile] = File(...)):
        authorize(request)
        job = owned(job_id, request)
        if job['state'] != 'awaiting_papers':
            raise HTTPException(409, 'Only a task awaiting papers can be resumed here.')
        if store.pending() >= int(os.getenv('STUDIO_MAX_PENDING', '5')):
            raise HTTPException(429, 'The generation queue is full.')
        detail = public(job, True)
        expected = {p.get('expected_pdf_filename') for p in detail.get('missing_papers', [])}
        expected = {p for p in expected if p and Path(p).name == p}
        if not files:
            raise HTTPException(422, 'Attach at least one requested PDF.')
        async with queue.enqueue_lock:
            if store.get(job_id)['state'] != 'awaiting_papers':
                raise HTTPException(409, 'This task has already been resumed.')
            if store.pending() >= int(os.getenv('STUDIO_MAX_PENDING', '5')):
                raise HTTPException(429, 'The generation queue is full.')
            await save_files(files, store.root / job_id / 'papers', True, expected)
            write_json(store.root / job_id / 'status.json', {'state': 'queued', 'stage': 'protocol_pdf_ingest'})
            store.update(job_id, 'queued')
        return public(store.get(job_id))

    @app.post('/api/jobs/{job_id}/cancel')
    async def cancel(job_id: str, request: Request):
        job = owned(job_id, request)
        if job['state'] not in ('queued', 'running'):
            raise HTTPException(409, 'This task is not running.')
        store.update(job_id, 'cancelled')
        if queue.active == job_id:
            terminate(queue.process)
        return public(store.get(job_id))

    @app.delete('/api/jobs/{job_id}', status_code=204)
    async def delete(job_id: str, request: Request):
        job = owned(job_id, request)
        if job['state'] in ('running', 'queued') or queue.active == job_id:
            raise HTTPException(409, 'Cancel the task and wait for it to stop before deleting it.')
        shutil.rmtree(store.root / job_id, ignore_errors=True)
        store.delete(job_id)
        return Response(status_code=204)

    @app.get('/api/jobs/{job_id}/export')
    async def export(job_id: str, request: Request):
        owned(job_id, request)
        path = store.root / job_id / 'protocol.md'
        if not path.exists():
            raise HTTPException(404, 'No draft has been saved yet.')
        return FileResponse(path, media_type='text/markdown', filename='biomars-protocol.md')

    # Deliberately mount only static assets, never the repo or task storage.
    app.mount('/', StaticFiles(directory=ROOT / 'web', html=True), name='web')
    return app


app = create_app()
