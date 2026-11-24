import requests

ODOO_URL = "http://localhost:8069"
ODOO_DB = ""
API_KEY = ""

headers = {
    "Authorization": f"bearer {API_KEY}",
    "X-Odoo-Database": ODOO_DB,
}

body = {
    "vals_list": [
        {
            "name": "Test event from Python",
            "event_date": "2026-12-01 18:00:00",
            "location": "Vienna",
            "url": "https://example.com",
        }
    ]
}

response = requests.post(
    f"{ODOO_URL}/json/2/event.notifier/create",
    headers=headers,
    json=body,
)

print(response.status_code)
print(response.text)