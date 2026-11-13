"""Start the Studio for personal use on this computer only."""
import os
import uvicorn


if __name__ == '__main__':
    os.environ['STUDIO_LOCAL_MODE'] = '1'
    uvicorn.run('studio.api:app', host='127.0.0.1', port=8745, proxy_headers=False)
