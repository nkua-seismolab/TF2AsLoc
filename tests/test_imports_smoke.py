"""
Smoke tests for package imports and new tf2asloc package structure.

Usage:
    pytest tests/test_imports_smoke.py -v
"""

import pytest


class TestTf2aslocPackageStructure:
    """Verify the new package layout imports correctly."""

    def test_import_config_loader(self):
        from tf2asloc.config.loader import load_config

        assert callable(load_config)

    def test_import_db_models(self):
        from tf2asloc.db.models import Pick, SystemState

        assert Pick.__tablename__ == "picks"
        assert SystemState.__tablename__ == "system_state"

    def test_import_db_session(self):
        from tf2asloc.db.session import init_db

        assert callable(init_db)

    def test_import_api_app(self, monkeypatch):
        # app config requires DB credentials at import time
        monkeypatch.setenv("POSTGRES_USER", "test")
        monkeypatch.setenv("POSTGRES_PASSWORD", "test")
        monkeypatch.setenv("POSTGRES_DB", "test")
        # other tests may leave TF2ASLOC_CONFIG pointing at a missing file
        from pathlib import Path

        repo_root = Path(__file__).resolve().parent.parent
        monkeypatch.setenv("TF2ASLOC_CONFIG", str(repo_root / "config.example.yaml"))
        from tf2asloc.api.app import create_app

        assert callable(create_app)

    def test_import_api_routes(self):
        from tf2asloc.api.routes.picks import router as picks_router

        assert picks_router is not None

    def test_import_workers(self):
        from tf2asloc.workers import associate, calculate_magnitude, locate

        assert callable(associate.run_association)
        assert callable(locate.run_location)
        assert callable(calculate_magnitude.run_magnitude)

    def test_import_utils_helpers(self):
        from tf2asloc.utils.helpers import (
            calculate_distance,
        )

        assert callable(calculate_distance)

    def test_import_orchestrator(self):
        from tf2asloc.orchestrator.orchestrator import run

        assert callable(run)

    def test_import_logger(self):
        from tf2asloc.logger.logger import setup_logging

        assert callable(setup_logging)


class TestCoreScientificPackages:
    """Test core scientific computing packages (numpy, pandas, scipy)."""

    def test_import_numpy(self):
        """Test numpy>=1.24.0 import and basic functionality."""
        import numpy as np

        assert hasattr(np, "__version__")
        assert hasattr(np, "array")
        arr = np.array([1, 2, 3])
        assert len(arr) == 3

    def test_import_pandas(self):
        """Test pandas>=2.0.0 import and basic functionality."""
        import pandas as pd

        assert hasattr(pd, "__version__")
        assert hasattr(pd, "DataFrame")
        df = pd.DataFrame({"a": [1, 2, 3]})
        assert len(df) == 3

    def test_import_scipy(self):
        """Test scipy>=1.11.0 import."""
        import scipy

        assert hasattr(scipy, "__version__")
        # Test a common submodule
        from scipy import stats

        assert stats is not None


class TestSeismologyPackages:
    """Test seismology-specific packages (obspy)."""

    def test_import_obspy(self):
        """Test obspy>=1.4.0 import and core functionality."""
        import obspy

        assert hasattr(obspy, "__version__")

        # Test essential submodules used in the codebase
        from obspy import UTCDateTime, read_events
        from obspy.core.event import (
            Catalog,
            Event,
        )
        from obspy.core.inventory import Inventory

        assert UTCDateTime is not None
        assert Inventory is not None
        assert Catalog is not None
        assert Event is not None
        assert read_events is not None


class TestAssociationPackages:
    """Test phase association packages (gamma-associator)."""

    def test_import_gamma(self):
        """Test gamma-associator (from GitHub) import."""
        # gamma is installed from: git+https://github.com/wayneweiqiang/GaMMA.git
        from gamma.utils import association

        assert association is not None

        # Verify it has the expected functionality
        assert hasattr(association, "BayesianGaussianMixture") or callable(association)


class TestGeospatialPackages:
    """Test geospatial/projection packages (pyproj)."""

    def test_import_pyproj(self):
        """Test pyproj>=3.6.0 import and basic functionality."""
        from pyproj import CRS, Transformer

        assert CRS is not None
        assert Transformer is not None

        # Basic smoke test - create a simple CRS
        crs = CRS.from_epsg(4326)  # WGS84
        assert crs.is_geographic


class TestUIProgressPackages:
    """Test UI/progress bar packages (tqdm)."""

    def test_import_tqdm(self):
        """Test tqdm import and basic functionality."""
        from tqdm import tqdm

        assert tqdm is not None

        # Basic smoke test
        items = list(tqdm(range(3), disable=True))
        assert len(items) == 3


class TestDatabasePackages:
    """Test database packages (SQLAlchemy, psycopg2, python-dotenv)."""

    def test_import_sqlalchemy(self):
        """Test SQLAlchemy>=2.0.0 import and core functionality."""
        import sqlalchemy
        from sqlalchemy import create_engine
        from sqlalchemy.orm import declarative_base, sessionmaker

        assert hasattr(sqlalchemy, "__version__")
        assert create_engine is not None
        assert declarative_base is not None
        assert sessionmaker is not None

        # Basic smoke test - create in-memory database
        engine = create_engine("sqlite:///:memory:")
        assert engine is not None

    def test_import_psycopg2(self):
        """Test psycopg2-binary import."""
        import psycopg2

        assert hasattr(psycopg2, "__version__")
        assert hasattr(psycopg2, "connect")

    def test_import_dotenv(self):
        """Test python-dotenv import."""
        from dotenv import find_dotenv, load_dotenv

        assert load_dotenv is not None
        assert find_dotenv is not None


class TestAPIPackages:
    """Test API framework packages (FastAPI, uvicorn, httpx, pydantic)."""

    def test_import_fastapi(self):
        """Test FastAPI>=0.100.0 import and basic functionality."""
        from fastapi import FastAPI

        assert FastAPI is not None

        # Basic smoke test - create app instance
        app = FastAPI()
        assert app is not None

    def test_import_uvicorn(self):
        """Test uvicorn>=0.24.0 import."""
        import uvicorn

        assert hasattr(uvicorn, "run")

    def test_import_httpx(self):
        """Test httpx>=0.25.0 import and basic functionality."""
        import httpx

        assert hasattr(httpx, "Client")
        assert hasattr(httpx, "AsyncClient")

    def test_import_pydantic(self):
        """Test pydantic>=2.0.0 import and basic functionality."""
        from pydantic import BaseModel

        assert BaseModel is not None

        # Basic smoke test - create a model
        class TestModel(BaseModel):
            value: int

        obj = TestModel(value=42)
        assert obj.value == 42


class TestCLIPackages:
    """Test CLI framework packages (click)."""

    def test_import_click(self):
        """Test click>=8.1.0 import and basic functionality."""
        import click

        assert hasattr(click, "command")
        assert hasattr(click, "option")

        # Basic smoke test - create a command
        @click.command()
        def dummy():
            pass

        assert dummy is not None


class TestTestingPackages:
    """Test testing framework packages (pytest)."""

    def test_import_pytest(self):
        """Test pytest>=7.4.0 import."""
        assert hasattr(pytest, "__version__")
        assert hasattr(pytest, "mark")
        assert hasattr(pytest, "fixture")


# Comprehensive smoke test function for all requirements.txt packages
def test_all_requirements_smoke():
    """
    Comprehensive smoke test for ALL packages in requirements.txt.

    This test validates every package listed in requirements.txt can be imported.
    NO packages are optional - all must be available for this test to pass.

    Required packages from requirements.txt:
    - numpy>=1.24.0
    - pandas>=2.0.0
    - scipy>=1.11.0
    - obspy>=1.4.0
    - gamma-associator (from GitHub)
    - pyproj>=3.6.0
    - tqdm
    - sqlalchemy>=2.0.0
    - psycopg2-binary
    - python-dotenv>=1.0.0
    - fastapi>=0.100.0
    - uvicorn>=0.24.0
    - httpx>=0.25.0
    - click>=8.1.0
    - pydantic>=2.0.0
    - pytest>=7.4.0
    """
    failures = []

    # Test each package
    packages_to_test = [
        ("numpy", "import numpy"),
        ("pandas", "import pandas"),
        ("scipy", "import scipy"),
        ("obspy", "import obspy"),
        ("gamma", "from gamma.utils import association"),
        ("pyproj", "from pyproj import CRS"),
        ("tqdm", "from tqdm import tqdm"),
        ("sqlalchemy", "import sqlalchemy"),
        ("psycopg2", "import psycopg2"),
        ("dotenv", "from dotenv import load_dotenv"),
        ("fastapi", "from fastapi import FastAPI"),
        ("uvicorn", "import uvicorn"),
        ("httpx", "import httpx"),
        ("click", "import click"),
        ("pydantic", "from pydantic import BaseModel"),
        ("pytest", "import pytest"),
    ]

    for package_name, import_statement in packages_to_test:
        try:
            exec(import_statement)
        except ImportError as e:
            failures.append(f"{package_name}: {e}")

    # Assert all imports succeeded
    if failures:
        error_msg = "The following packages from requirements.txt failed to import:\n"
        error_msg += "\n".join(f"  ❌ {f}" for f in failures)
        error_msg += "\n\nInstall missing packages with:\n  pip install -r requirements.txt"
        raise ImportError(error_msg)


if __name__ == "__main__":
    # Run comprehensive smoke test directly
    print("=" * 70)
    print("Running comprehensive import smoke test for requirements.txt")
    print("=" * 70)
    print("\nTesting all packages listed in requirements.txt...")
    print("(All packages are required - no optional dependencies)\n")

    try:
        test_all_requirements_smoke()
        print("✅ SUCCESS: All packages from requirements.txt imported successfully!")
        print("\nVerified packages:")
        print("  • Core Scientific: numpy, pandas, scipy")
        print("  • Seismology: obspy")
        print("  • Association: gamma-associator")
        print("  • Geospatial: pyproj")
        print("  • UI: tqdm")
        print("  • Database: sqlalchemy, psycopg2-binary, python-dotenv")
        print("  • API: fastapi, uvicorn, httpx, pydantic")
        print("  • CLI: click")
        print("  • Testing: pytest")
        exit(0)
    except ImportError as e:
        print("\n❌ FAILURE: Some packages failed to import\n")
        print(str(e))
        exit(1)
