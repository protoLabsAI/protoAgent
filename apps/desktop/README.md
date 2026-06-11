# ProtoAgent Desktop

## Platforms & CI

### Windows

The Windows build is generated using PyInstaller and packaged as a single executable.

**Code Signing:**
To prevent SmartScreen warnings and AV false positives, all Windows releases are automatically code-signed during the CI/CD pipeline using Azure Key Vault.

- **Requirement:** A valid Azure Key Vault certificate with code signing permissions must be configured in the repository secrets (`AZURE_CREDENTIALS`, `AZURE_KEY_VAULT_NAME`, `AZURE_CERT_NAME`).
- **Process:** The `release.yml` workflow handles the signing step automatically for tagged releases.
- **Verification:** Users can verify the signature by right-clicking the `.exe` -> Properties -> Digital Signatures.

> **Note:** Unsigned builds are disabled for production releases. If you are building locally for development, you may encounter SmartScreen prompts.

### macOS & Linux

(Existing content for other platforms...)