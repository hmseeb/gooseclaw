"""E2E Docker container lifecycle fixtures.

Separate from conftest.py to avoid polluting unit test fixtures.
These fixtures build the real Docker image and manage container lifecycle.
"""

import os
import subprocess
import time
import uuid

import pytest


# ---------------------------------------------------------------------------
# Auto-skip when Docker is unavailable
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True, scope="module")
def skip_if_no_docker():
    """Skip the entire module when Docker daemon is unreachable."""
    try:
        subprocess.run(
            ["docker", "info"],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        pytest.skip("Docker not available")


# ---------------------------------------------------------------------------
# Docker image (session-scoped, built once)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session")
def docker_image():
    """Build the GooseClaw Docker image from the project Dockerfile.

    Yields the image tag.  Removes the image on teardown (best-effort).
    """
    tag = "gooseclaw-e2e-test"
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

    result = subprocess.run(
        ["docker", "build", "-t", tag, "."],
        cwd=project_root,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        pytest.fail(f"Docker build failed:\n{result.stderr}")

    yield tag

    # Teardown: remove the image (best-effort)
    subprocess.run(
        ["docker", "rmi", "-f", tag],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ---------------------------------------------------------------------------
# Docker container (function-scoped, fresh per test)
# ---------------------------------------------------------------------------

@pytest.fixture
def docker_container(docker_image):
    """Run a GooseClaw container with a random host port mapped to 8080.

    Yields a dict:
        {
            "base_url": "http://localhost:<port>",
            "container_name": "<name>",
            "container_id": "<id>",
        }

    Stops and removes the container on teardown (best-effort).
    """
    name = f"gooseclaw-e2e-{uuid.uuid4().hex[:8]}"

    # Start container in detached mode with random host port
    run_result = subprocess.run(
        [
            "docker", "run", "-d",
            "--name", name,
            "-p", "0:8080",
            "-e", "GOOSE_PROVIDER=openai",
            "-e", "OPENAI_API_KEY=sk-fake-test-key-not-real",
            docker_image,
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if run_result.returncode != 0:
        pytest.fail(f"docker run failed:\n{run_result.stderr}")

    container_id = run_result.stdout.strip()

    # Discover the mapped host port
    port_result = subprocess.run(
        ["docker", "port", name, "8080"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if port_result.returncode != 0:
        # Cleanup before failing
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pytest.fail(f"docker port failed:\n{port_result.stderr}")

    # Output like "0.0.0.0:32789\n" or ":::32789\n"
    raw_port = port_result.stdout.strip().split(":")[-1]
    host_port = int(raw_port)

    base_url = f"http://localhost:{host_port}"

    # Wait up to 90 seconds for the container to become reachable
    import requests as req

    deadline = time.time() + 90
    reachable = False
    while time.time() < deadline:
        try:
            resp = req.get(f"{base_url}/api/health", timeout=3)
            if resp.status_code == 200:
                reachable = True
                break
        except Exception:
            pass
        time.sleep(2)

    if not reachable:
        # Dump container logs for debugging
        logs = subprocess.run(
            ["docker", "logs", "--tail", "50", name],
            capture_output=True, text=True,
        )
        subprocess.run(["docker", "rm", "-f", name], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        pytest.fail(
            f"Container {name} did not become reachable within 90s.\n"
            f"Last 50 lines of logs:\n{logs.stdout}\n{logs.stderr}"
        )

    yield {
        "base_url": base_url,
        "container_name": name,
        "container_id": container_id,
    }

    # Teardown: stop and remove (best-effort)
    subprocess.run(
        ["docker", "stop", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
    subprocess.run(
        ["docker", "rm", "-f", name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=15,
    )
