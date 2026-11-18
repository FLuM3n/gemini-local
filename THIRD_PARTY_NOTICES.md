# Third-party components

`vendor/gemini_webapi/` contains the gemini-webapi 2.1.1 Python source snapshot used by the original standalone Windows package. The distribution metadata lists the repository as https://github.com/HanaokaYuzu/Gemini-API and its author as UZQueen. Its AGPL-3.0 license is preserved in that directory and at the project root.

The snapshot is included to keep model discovery, account status, browser-session handling and image support consistent across Windows and macOS. Native runtime dependencies are installed from `requirements.txt`; their licenses remain with their distributions.

Windows portable release assets additionally contain the original Python runtime and its dependency licenses in `licenses/` and the installed package metadata. These native Windows files are not part of the source repository or the macOS archive.

This project is not an official Google product.
