# Optional Private Protocol Registry

LIF2TIFF keeps general software and project-specific acquisition evidence
separate. Public packages contain `channel_registry.json` and an empty
`protocol_registry.json` template. Conversion, preview, validation, and manual
channel confirmation remain available without a private registry.

When present, a private registry adds historical protocol-family matches as
unconfirmed dye candidates.

## Default locations

- Linux: `~/.config/lif2tiff/protocol_registry.json`
- Windows: `%APPDATA%\LIF2TIFF\protocol_registry.json`

Set `LIF2TIFF_PROTOCOL_REGISTRY` to use another file for one shell or managed
deployment. The environment override takes priority over the user config file;
the bundled public template is the final fallback.

An invalid or missing explicitly configured file raises an error rather than
silently using another registry. Registry paths are runtime configuration and
are not written into portable conversion manifests.

The private registry should be versioned separately with restricted access. It
must not contain raw LIF pixels, credentials, or clinical identifiers.
