/* Shared display-only Markdown rendering for the workspace and quick panel. */
(function (root, factory) {
  if (typeof module === 'object' && module.exports) {
    module.exports = factory(require('./vendor/markdown-it-15.0.3.min.js'));
  } else {
    root.NovaMindMarkdown = factory(root.markdownit);
  }
})(typeof globalThis !== 'undefined' ? globalThis : this, function (markdownit) {
  'use strict';

  // Model output is untrusted. Keep raw HTML disabled and retain the parser's
  // URL validation (including its javascript:, file: and data: restrictions).
  const parser = markdownit({html: false, breaks: true, linkify: false, typographer: false});
  parser.renderer.rules.link_open = function (tokens, index, options, env, renderer) {
    tokens[index].attrSet('target', '_blank');
    tokens[index].attrSet('rel', 'noopener noreferrer');
    return renderer.renderToken(tokens, index, options);
  };
  const renderImage = parser.renderer.rules.image;
  parser.renderer.rules.image = function (tokens, index, options, env, renderer) {
    tokens[index].attrSet('loading', 'lazy');
    tokens[index].attrSet('referrerpolicy', 'no-referrer');
    return renderImage(tokens, index, options, env, renderer);
  };

  return {render: source => parser.render(String(source ?? ''))};
});
