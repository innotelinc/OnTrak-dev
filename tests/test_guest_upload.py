"""Uploading a file into a guest, over the transport that has a command line.

`BaseDriver._write_bytes` sends a payload as base64 in chunks. On a Linux guest the
chunks ride a heredoc on stdin and size barely matters. On a Windows guest each chunk
becomes an argument of a PowerShell `-EncodedCommand`, and Windows caps the command
line the WinRM service builds -- so a chunk that is fine for one transport is refused
by the other with

    error: upload of C:\\ProgramData\\OnTrak\\post-install.ps1 failed at offset 0:
    The command line is too long.

which is what an 8 KiB `post-install.ps1` produced while `UPLOAD_CHUNK` was 32,000:
its 10,932 characters of base64 became roughly 14,600 characters of -EncodedCommand.
The tests here keep the two apart: the encoded command a full chunk produces has to
fit, and a payload of several chunks has to arrive byte for byte.
"""
from __future__ import annotations

import base64

from ontrak import guest

# The limit Windows refused the upload at. It is the guest's cmd.exe line (8191) that
# matters, and this is deliberately the same number rather than the measured failure
# point: the generated command has to stay under the limit, not merely near where it
# was seen to break.
COMMAND_LINE_LIMIT = 8191


class _RecordingWinRM(guest.WinRMDriver):
    """A WinRM driver that emulates the guest's side of the upload instead of TCP.

    It keeps every script it was asked to run -- which is how the command-line bound
    can be checked without a Windows machine -- and reassembles `Add-Content` chunks
    the way the file would have been assembled in the guest.
    """

    def __init__(self, settings):
        super().__init__(settings)
        self.scripts: list[str] = []
        self.chunks: list[str] = []
        self.decoded: bytes | None = None

    def run_powershell(self, script, host="", instance="", timeout=120):  # noqa: ARG002
        self.scripts.append(script)
        if "Add-Content" in script:
            start = script.index("-Value '") + len("-Value '")
            self.chunks.append(script[start : script.index("'", start)])
        elif "[Convert]::FromBase64String" in script:
            self.decoded = base64.b64decode("".join(self.chunks))
        return guest.CommandResult(True, 0, "", "", 0.0)


def test_the_encoded_command_a_full_chunk_produces_fits(settings):
    """The failure this guards against, at the width of one whole chunk.

    `_write_bytes` wraps a chunk in `Add-Content -Path '<file>' -Value '<chunk>' ...`,
    then `powershell_argv` encodes the whole thing. Building that worst case and
    measuring it is the only way to know the bound holds without a Windows guest.
    """
    worst_chunk = "A" * guest.WINRM_UPLOAD_CHUNK
    script = (
        r"Add-Content -Path 'C:\ProgramData\OnTrak\post-install.ps1.b64' "
        f"-Value '{worst_chunk}' -NoNewline -Encoding Ascii"
    )
    command_line = " ".join(guest.powershell_argv(script))
    assert len(command_line) < COMMAND_LINE_LIMIT, (
        f"a full chunk yields a {len(command_line)}-character command line; Windows "
        f"refuses anything over {COMMAND_LINE_LIMIT}"
    )


def test_the_winrm_chunk_is_smaller_than_the_default(settings):
    """Two transports, two widths -- and the narrow one is the default's job to know."""
    assert guest.WinRMDriver.upload_chunk < guest.UPLOAD_CHUNK


def test_a_payload_of_several_chunks_arrives_byte_for_byte(settings):
    """Several chunks, every command line under the limit, and the same bytes out.

    `post-install.ps1` is 8197 bytes, so it always takes more than one chunk at the
    WinRM width: the reassembly path is not hypothetical for it.
    """
    driver = _RecordingWinRM(settings)
    payload = bytes(range(256)) * 60  # 15,360 bytes, strictly larger than one chunk
    assert len(payload) > guest.WINRM_UPLOAD_CHUNK

    result = driver._write_bytes(payload, r"C:\ProgramData\OnTrak\post-install.ps1")

    assert result.ok
    assert len(driver.chunks) > 1, "the payload fitted in one chunk; this test proves nothing"
    for script in driver.scripts:
        assert len(" ".join(guest.powershell_argv(script))) < COMMAND_LINE_LIMIT
    assert driver.decoded == payload


def test_the_upload_still_lands_where_it_was_asked_to(settings):
    """Guarding the paths while the chunking is in hand: the decode names the target."""
    driver = _RecordingWinRM(settings)
    driver._write_bytes(b"hello", r"C:\ProgramData\OnTrak\thing.txt")
    decode = [s for s in driver.scripts if "[Convert]::FromBase64String" in s]
    assert decode, "no decode step was run"
    assert r"C:\ProgramData\OnTrak\thing.txt" in decode[0]
    assert r"C:\ProgramData\OnTrak\thing.txt.b64" in decode[0]
