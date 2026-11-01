// #528: hash-route query support — `#/aiagent?agent=<id>` style deep links.
// The App hash router splits on '/' only; this helper parses the query
// string off the first segment so pages can read deep-link params without
// a router dependency.
export function hashQuery(): URLSearchParams {
    if (typeof window === 'undefined') return new URLSearchParams();
    const h = window.location.hash.replace(/^#\/?/, '');
    const q = h.split('?')[1] ?? '';
    return new URLSearchParams(q);
}
