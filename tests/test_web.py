import hashlib

import httpx
import pytest

from oikb.connectors.web import WebConnector


def sync_pages(pages):
    origin = "https://wiki.example"
    responses = dict(pages)

    def respond(request):
        text = responses[request.url.raw_path.decode()]
        return httpx.Response(404 if text is None else 200, text=text)

    with WebConnector(origin, delay=0) as connector:
        connector._http.close()
        connector._http = httpx.Client(transport=httpx.MockTransport(respond))
        connector._discover_urls = lambda: [origin + path for path, _ in pages]
        return {
            entry.display_path: connector.read_file(entry.path, entry.filename).decode()
            for entry in connector.build_manifest()
        }


def test_reads_the_exact_page_listed_in_the_manifest():
    assert sync_pages([
        ("/NotHauptseite", "Different page"),
        ("/Hauptseite", "Home"),
        ("/Unterst%C3%BCtzte_Hardware", "Hardware"),
        ("/Apo/Zahlungsweisen_(Webshop)", "Payments"),
        ("/notes.txt", "Notes"),
        ("/missing", None),
    ]) == {
        "NotHauptseite.txt": "Different page",
        "Hauptseite.txt": "Home",
        "Unterst_C3_BCtzte_Hardware.txt": "Hardware",
        "Apo/Zahlungsweisen__Webshop_.txt": "Payments",
        "notes.txt": "Notes",
    }


@pytest.mark.parametrize("reverse", [False, True])
def test_names_for_colliding_urls_are_unique_and_stable(reverse):
    suffix = hashlib.sha256(b"https://wiki.example/A(B)").hexdigest()[:12]
    reserved = f"A_B_-{suffix}"
    pages = [("/A(B)", "Parentheses"), ("/A_B_", "Underscores"), (f"/{reserved}", "Reserved")]
    assert sync_pages(pages[::-1] if reverse else pages) == {
        "A_B_.txt": "Underscores",
        f"{reserved}.txt": "Reserved",
        f"{reserved}_.txt": "Parentheses",
    }
