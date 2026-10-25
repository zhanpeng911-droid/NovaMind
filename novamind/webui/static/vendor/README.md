# Bundled Markdown renderer

`markdown-it-15.0.3.min.js` is the unchanged browser UMD distribution of
[markdown-it 15.0.3](https://github.com/markdown-it/markdown-it/releases/tag/15.0.3),
with a trailing newline added by the repository's patch tool.

- Source: <https://cdn.jsdelivr.net/npm/markdown-it@15.0.3/dist/browser/markdown-it.umd.min.js>
- Local SHA-256: `758061fd114ea825a7864ad8a4aeffa4ab97233b1ff9924f51e9bb635c0776f2`
- License and bundled dependency notices: `THIRD_PARTY_NOTICES.txt`.
- Runtime use is entirely local; there is no CDN request when opening the app.

The shared `../markdown.js` keeps HTML disabled and preserves the upstream URL
validator. See the upstream [safety guidance](https://github.com/markdown-it/markdown-it/blob/master/docs/safety.md).
Do not enable raw HTML or add plugins without reviewing their output handling.
The upstream source map is not shipped because it is not needed at runtime.
