/**
 * Mount layout HTML without assigning innerHTML on live DOM nodes.
 * Strips scripts and inline event handlers before insertion.
 */

function sanitizeParsedDocument(doc: Document): void {
  doc.querySelectorAll('script, iframe, object, embed').forEach(node => node.remove());
  doc.querySelectorAll('*').forEach(node => {
    for (const attr of Array.from(node.attributes)) {
      const name = attr.name.toLowerCase();
      const value = attr.value.trim().toLowerCase();
      if (name.startsWith('on')) {
        node.removeAttribute(attr.name);
        continue;
      }
      if ((name === 'href' || name === 'src') && value.startsWith('javascript:')) {
        node.removeAttribute(attr.name);
      }
    }
  });
}

function fragmentFromHtml(html: string): DocumentFragment {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  sanitizeParsedDocument(doc);
  const fragment = document.createDocumentFragment();
  fragment.append(...Array.from(doc.body.childNodes));
  return fragment;
}

export function mountLayoutHtml(element: HTMLElement, html: string): void {
  element.replaceChildren(...Array.from(fragmentFromHtml(html).childNodes));
}

export function replaceElementHtml(element: Element, html: string): void {
  const doc = new DOMParser().parseFromString(html, 'text/html');
  sanitizeParsedDocument(doc);
  const replacement = doc.body.firstElementChild;
  if (replacement) {
    element.replaceWith(replacement);
  }
}
