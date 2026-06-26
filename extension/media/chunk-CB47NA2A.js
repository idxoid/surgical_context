// src/webview/shared/domActions.ts
function bindClickAction(root, action, handler) {
  const button = root.querySelector(`[data-action="${action}"]`);
  if (button) {
    button.addEventListener("click", handler);
  }
}
function bindDataActions(root, handler) {
  root.querySelectorAll("[data-action]").forEach((element) => {
    element.addEventListener("click", handler);
  });
}

// src/webview/shared/domRender.ts
function sanitizeParsedDocument(doc) {
  doc.querySelectorAll("script, iframe, object, embed").forEach((node) => node.remove());
  doc.querySelectorAll("*").forEach((node) => {
    for (const attr of Array.from(node.attributes)) {
      const name = attr.name.toLowerCase();
      const value = attr.value.trim().toLowerCase();
      if (name.startsWith("on")) {
        node.removeAttribute(attr.name);
        continue;
      }
      if ((name === "href" || name === "src") && value.startsWith("javascript:")) {
        node.removeAttribute(attr.name);
      }
    }
  });
}
function fragmentFromHtml(html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  sanitizeParsedDocument(doc);
  const fragment = document.createDocumentFragment();
  fragment.append(...Array.from(doc.body.childNodes));
  return fragment;
}
function mountLayoutHtml(element, html) {
  element.replaceChildren(...Array.from(fragmentFromHtml(html).childNodes));
}
function replaceElementHtml(element, html) {
  const doc = new DOMParser().parseFromString(html, "text/html");
  sanitizeParsedDocument(doc);
  const replacement = doc.body.firstElementChild;
  if (replacement) {
    element.replaceWith(replacement);
  }
}

// src/webview/shared/webviewRuntime.ts
const vscode = acquireVsCodeApi();
const VSCODE_WEBVIEW_ORIGIN_PREFIX = "vscode-webview://";
function listenForHostMessages(handler) {
  const webviewOrigin = globalThis.location.origin;
  function receiveHostMessage(event) {
    if (event.origin !== webviewOrigin && event.origin !== "" && !event.origin.startsWith(VSCODE_WEBVIEW_ORIGIN_PREFIX)) {
      return;
    }
    handler(event.data);
  }
  window.addEventListener("message", receiveHostMessage);
}
function bootWebview(init) {
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
}

// src/webview/shared/html.ts
function escapeHtml(text) {
  const map = {
    "&": "&amp;",
    "<": "&lt;",
    ">": "&gt;",
    '"': "&quot;",
    "'": "&#039;"
  };
  return text.replace(/[&<>"']/g, (char) => map[char]);
}

export {
  bindClickAction,
  bindDataActions,
  mountLayoutHtml,
  replaceElementHtml,
  vscode,
  listenForHostMessages,
  bootWebview,
  escapeHtml
};
//# sourceMappingURL=chunk-CB47NA2A.js.map
