from pathlib import Path
from django.http import HttpResponse,JsonResponse

def manifest(request):
    return JsonResponse({'id':'/','name':'GesTi · Gestão de fluxo','short_name':'GesTi','description':'Acompanhe e analise o fluxo dos seus acessos.','lang':'pt-BR','start_url':'/','scope':'/','display':'standalone','background_color':'#0a1220','theme_color':'#0a1220','icons':[{'src':'/static/gesti/icon.svg','sizes':'any','type':'image/svg+xml','purpose':'any'},{'src':'/static/gesti/icon-192.png','sizes':'192x192','type':'image/png','purpose':'any maskable'},{'src':'/static/gesti/icon-512.png','sizes':'512x512','type':'image/png','purpose':'any maskable'}]},content_type='application/manifest+json')

def worker(request):
    response=HttpResponse((Path(__file__).parent/'static/gesti/sw.js').read_text(),content_type='text/javascript')
    response['Cache-Control']='no-cache';response['Service-Worker-Allowed']='/';return response
