"""Client for the enrichment API of an AmiGO 3 server.

AmiGO 3 (https://github.com/geneontology/amigo3-prototype) tests a gene list
against GO terms (hypergeometric or MGSA), GO-CAM models and GO-CAM network
neighborhoods. This client talks to a running server over HTTP, so TALISMAN does
not depend on the amigo3 package or its Python version.
"""

import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import quote

import requests

logger = logging.getLogger(__name__)

DEFAULT_AMIGO3_URL = "http://127.0.0.1:8030"

# TALISMAN method name -> target and method of amigo3's POST /api/enrichment
METHODS: Dict[str, Dict[str, str]] = {
    "go": {"target": "go", "method": "hypergeometric"},
    "mgsa": {"target": "go", "method": "mgsa"},
    "gocam": {"target": "gocam", "method": "hypergeometric"},
    "neighborhood": {"target": "neighborhood", "method": "neighborhood"},
    "network": {"target": "go", "method": "network"},
}
DEFAULT_METHODS = ["go", "mgsa", "gocam", "neighborhood"]


class Amigo3Error(Exception):
    """The AmiGO 3 server rejected a request or could not be reached."""


@dataclass
class Amigo3Client:
    """Minimal client for the AmiGO 3 enrichment endpoints."""

    url: str = DEFAULT_AMIGO3_URL
    timeout: float = 600
    session: requests.Session = field(default_factory=requests.Session)

    def options(self) -> dict:
        """Return the pinned build ID and the species, methods and presets on offer."""
        return self._request("get", "/api/enrichment")

    def enrich(self, genes: List[str], species: str, method: str = "go", **parameters) -> dict:
        """Run one enrichment method on a gene list.

        :param genes: gene identifiers or symbols; amigo3 resolves them within the species
        :param species: an amigo3 species key, e.g. human, pombe, worm, fly
        :param method: a key of METHODS
        :param parameters: other EnrichmentRequest fields, e.g. aspect, evidence, network_taxon
        """
        if method not in METHODS:
            raise ValueError(f"Unknown method {method!r}; choose from {', '.join(METHODS)}")
        body = {"genes": list(genes), "species": species, **METHODS[method], **parameters}
        return self._request("post", "/api/enrichment", json=body)

    def example_genesets(self, species: str) -> List[Dict]:
        """Bundled example gene sets for a species, each with its curated expected GO terms."""
        return self._request("get", "/api/genesets", params={"species": species})["examples"]

    def geneset(self, identifier: str, species: str) -> Dict:
        """Map a gene set onto the build; the returned "genes" list is ready for enrich()."""
        path = "/api/genesets/" + quote(identifier, safe="")
        return self._request("get", path, params={"species": species})

    def _request(self, verb: str, path: str, **kwargs) -> dict:
        url = self.url.rstrip("/") + path
        try:
            response = self.session.request(verb, url, timeout=self.timeout, **kwargs)
        except requests.RequestException as e:
            raise Amigo3Error(f"Could not reach AmiGO 3 at {self.url}: {e}") from e
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            raise Amigo3Error(f"{verb.upper()} {path} returned {response.status_code}: {detail}")
        return response.json()


def run_methods(
    client: Amigo3Client,
    genes: List[str],
    species: str,
    methods: List[str],
    parameters: Optional[Dict] = None,
) -> Dict[str, Dict]:
    """Run several methods; a method the build cannot run is recorded, not raised.

    Parameters go to every method. amigo3 ignores those that do not apply, e.g. the
    GO aspect for GO-CAM models.

    :return: {"results": {method: response}, "skipped": {method: reason}}
    """
    results, skipped = {}, {}
    for method in methods:
        try:
            results[method] = client.enrich(genes, species, method, **(parameters or {}))
        except Amigo3Error as e:
            logger.warning(f"Skipping {method}: {e}")
            skipped[method] = str(e)
    return {"results": results, "skipped": skipped}
