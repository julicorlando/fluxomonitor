// Never store authenticated pages, tokens, API data or floor plans offline.
self.addEventListener('install',()=>self.skipWaiting());
self.addEventListener('activate',event=>event.waitUntil(self.clients.claim()));
self.addEventListener('fetch',event=>{
 if(event.request.mode==='navigate'&&event.request.method==='GET')event.respondWith(fetch(event.request).catch(()=>new Response('<!doctype html><html lang="pt-BR"><meta name="viewport" content="width=device-width"><title>GesTi · Sem conexão</title><body style="background:#0a1220;color:#eef3fc;font:18px system-ui;padding:40px"><h1>GesTi</h1><h2>Você está sem conexão</h2><p>Conecte-se à internet para consultar dados atualizados.</p><button onclick="location.reload()">Tentar novamente</button></body></html>',{status:503,headers:{'Content-Type':'text/html;charset=utf-8'}})));
});
