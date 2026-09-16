from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.app.frontend import mount_frontend
from backend.app.routers.enroll import router


def test_dashboard_routes_and_backend_boundaries(tmp_path):
    (tmp_path / 'index.html').write_text('<title>AZKT Dashboard</title>')
    (tmp_path / 'assets').mkdir()
    (tmp_path / 'assets/app.js').write_text('console.log("dashboard")')
    app = FastAPI()
    app.include_router(router)

    @app.get('/auth/state')
    def state():
        return {'authed': False}

    mount_frontend(app, tmp_path)
    with TestClient(app) as client:
        for path in ['/', '/vehicles', '/vehicles/example', '/login']:
            response = client.get(path)
            assert response.status_code == 200
            assert 'AZKT Dashboard' in response.text
        assert 'Open the dashboard' in client.get('/enroll').text
        assert client.get('/assets/app.js').text == 'console.log("dashboard")'
        assert client.get('/auth/state').json() == {'authed': False}
        for path in ['/api/missing', '/auth/missing', '/mcp/missing', '/assets/missing.js', '/%2e%2e/private']:
            assert client.get(path).status_code == 404


def test_unbuilt_frontend_keeps_enrollment_available(tmp_path):
    app = FastAPI()
    app.include_router(router)
    mount_frontend(app, tmp_path)
    with TestClient(app) as client:
        response = client.get('/', follow_redirects=False)
        assert response.status_code == 307
        assert response.headers['location'] == '/enroll'
