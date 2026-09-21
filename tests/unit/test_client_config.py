from __future__ import annotations

import base64
import json

from rustdesk_api.services import client_config
from rustdesk_api.services.client_config import ClientServers


def test_the_config_string_is_reversed_urlsafe_base64_of_the_clients_json():
    servers = ClientServers("id.example.com", "relay.example.com", "https://api.example.com", "abc+/=key")
    text = client_config.config_string(servers)

    # The client's decoder: reverse, restore padding, URL-safe base64, JSON.
    reversed_text = text[::-1]
    raw = base64.urlsafe_b64decode(reversed_text + "=" * (-len(reversed_text) % 4))
    assert json.loads(raw) == {
        "host": "id.example.com",
        "relay": "relay.example.com",
        "api": "https://api.example.com",
        "key": "abc+/=key",
    }
    assert "=" not in text
    assert client_config.decode_config_string(text)["host"] == "id.example.com"


def test_empty_optional_fields_are_still_present_as_the_client_writes_them():
    text = client_config.config_string(ClientServers("id.example.com"))
    assert client_config.decode_config_string(text) == {
        "host": "id.example.com",
        "relay": "",
        "api": "",
        "key": "",
    }


def test_the_file_name_form_has_only_the_parts_that_are_set_and_ends_with_a_comma():
    servers = ClientServers("id.example.com", "", "https://api.example.com", "K3Y")
    assert (
        client_config.exe_name(servers)
        == "rustdesk-host=id.example.com,key=K3Y,api=https://api.example.com,.exe"
    )
    assert client_config.exe_name(ClientServers("id.example.com")) == "rustdesk-host=id.example.com,.exe"


def test_warnings_name_settings_that_will_not_do_what_is_expected():
    fine = ClientServers("id.example.com", "", "https://api.example.com", "key")
    assert client_config.warnings(fine) == []
    assert "https_21114" in client_config.warnings(
        ClientServers("i", "", "https://api.example.com:21114", "k")
    )
    assert "api_without_scheme" in client_config.warnings(ClientServers("i", "", "api.example.com", "k"))
    assert "no_key" in client_config.warnings(ClientServers("i", "", "http://a", ""))
    # Plain http on 21114 is what the client handles fine.
    assert client_config.warnings(ClientServers("i", "", "http://api.example.com:21114", "k")) == []
