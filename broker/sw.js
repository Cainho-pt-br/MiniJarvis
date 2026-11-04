// Abre o app instalado no celular sem esperar o Wi-Fi fraco do Pi: a casca
// (páginas, ícones, logos) sai do cache na hora e é atualizada por trás para
// a próxima abertura. Dados (/api/...), fotos e o que for de fora vão sempre
// à rede. O mesmo arquivo está em broker/, crm/ e maquina/
// (cerebro/apps-no-celular.md).
const CACHE = 'casca-v6';

self.addEventListener('install', () => self.skipWaiting());
self.addEventListener('activate', e => e.waitUntil(
  caches.keys()
    .then(ks => Promise.all(ks.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .catch(() => {})
    .then(() => self.clients.claim())));

self.addEventListener('fetch', e => {
  const r = e.request, u = new URL(r.url);
  if (r.method !== 'GET' || u.origin !== location.origin) return;
  const casca = r.mode === 'navigate' || /^\/(icones\/[^/]+|[^/]+\.png)$/.test(u.pathname);
  if (!casca) return;
  e.respondWith((async () => {
    // O cache é só atalho: se ele falhar (disco cheio, perfil corrompido),
    // segue pela rede como se não houvesse service worker.
    let c = null, guardado;
    try { c = await caches.open(CACHE); guardado = await c.match(r); } catch (_) {}
    const rede = fetch(r).then(resp => {
      if (resp.ok && c) c.put(r, resp.clone()).catch(() => {});
      // A casca guardada abriu na hora, então esta página está numa versão
      // velha. Se a que chegou agora é outra, avisa a página, que oferece
      // recarregar — senão a atualização só apareceria na próxima abertura,
      // sem ninguém saber que existe.
      const velha = guardado && (guardado.headers.get('etag') || guardado.headers.get('last-modified'));
      const nova = resp.headers.get('etag') || resp.headers.get('last-modified');
      if (r.mode === 'navigate' && resp.ok && velha && nova && velha !== nova) {
        self.clients.matchAll({type: 'window'})
          .then(cs => cs.forEach(cl => cl.postMessage({tipo: 'nova-versao'}))).catch(() => {});
      }
      return resp;
    });
    if (!guardado) return rede;
    e.waitUntil(rede.catch(() => {}));
    return guardado;
  })());
});
