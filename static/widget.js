(function() {
  var script = document.currentScript;
  var slug = script.getAttribute('data-slug');
  if (!slug) { console.error('WSIC widget: data-slug attribute is required'); return; }

  var host = script.src.split('/static/widget.js')[0] || 'https://whatshouldicharge.app';
  var url = host + '/estimate/' + encodeURIComponent(slug);

  var container = document.createElement('div');
  container.style.cssText = 'width:100%;max-width:640px;margin:0 auto;';

  var iframe = document.createElement('iframe');
  iframe.src = url;
  iframe.style.cssText = 'width:100%;min-height:700px;border:none;border-radius:12px;';
  iframe.setAttribute('loading', 'lazy');
  iframe.setAttribute('title', 'Get a Free Junk Removal Estimate');
  iframe.setAttribute('allow', 'camera');

  container.appendChild(iframe);
  script.parentNode.insertBefore(container, script.nextSibling);

  // Auto-resize iframe based on content height
  window.addEventListener('message', function(e) {
    if (e.data && e.data.type === 'wsic-resize' && e.data.height) {
      iframe.style.height = e.data.height + 'px';
    }
  });
})();
