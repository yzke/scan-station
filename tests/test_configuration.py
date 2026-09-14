import os

import pytest

from configuration import apply_environment_file, configured_port, read_environment_file


def test_environment_file_preserves_synthetic_credential_punctuation_without_evaluation(tmp_path, monkeypatch):
    # This is deliberately synthetic fixture text, never a real credential.
    path = tmp_path / "local.env"
    path.write_text("# local example\nSCAN_STATION_PORT='18081'\n"
                    "SCAN_STATION_SMB_PASSWORD='synthetic $value # punctuation'\n")
    values = read_environment_file(path)
    assert values["SCAN_STATION_SMB_PASSWORD"] == "synthetic $value # punctuation"
    assert configured_port(values) == 18081
    monkeypatch.setenv("SCAN_STATION_SMB_PASSWORD", "synthetic previous value")
    monkeypatch.setenv("SCAN_STATION_PORT", "8081")
    apply_environment_file(path)
    assert os.environ["SCAN_STATION_SMB_PASSWORD"] == values["SCAN_STATION_SMB_PASSWORD"]
    assert configured_port() == 18081


@pytest.mark.parametrize("text", ["SCAN_STATION_PORT='unclosed", "UNKNOWN_SETTING=value", "invalid line"])
def test_bad_environment_file_reports_line_without_echoing_value(tmp_path, text):
    path = tmp_path / "invalid.env"
    path.write_text(text)
    with pytest.raises(ValueError) as result:
        read_environment_file(path)
    assert "1" in str(result.value)
    assert text not in str(result.value)


@pytest.mark.parametrize("value", ["", "0", "65536", "not-a-port", "80.5"])
def test_invalid_port_is_rejected(value):
    with pytest.raises(ValueError):
        configured_port({"SCAN_STATION_PORT": value})


def test_missing_smb_configuration_fails_before_opening_a_connection(monkeypatch):
    import server

    monkeypatch.setattr(server, "SCANNER_HOST", "")
    monkeypatch.setattr(server, "SMB_USER", "")
    monkeypatch.setattr(server, "SMB_PASS", "")
    monkeypatch.setattr(server, "SMBConnection", lambda *args, **kwargs: pytest.fail("Network was attempted"))
    with pytest.raises(RuntimeError, match="SCAN_STATION_SCANNER_HOST"):
        server.smb()
