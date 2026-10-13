"""Select upstream Edge transport without modifying installed SDK files.

The msedgeservices endpoint/protocol is from edge-tts 7.2.3's public
constants. Bing returns 403 on this Windows connection; the alternate
endpoint was independently tested with the same Ava voice.
"""
import os


def configure_edge_transport():
    if os.environ.get('ATHENA_EDGE_TRANSPORT') != 'msedge': return
    import edge_tts.communicate as transport
    from edge_tts.constants import TRUSTED_CLIENT_TOKEN
    transport.WSS_URL = ('wss://api.msedgeservices.com/tts/cognitiveservices/websocket/v1'
        '?Ocp-Apim-Subscription-Key=' + TRUSTED_CLIENT_TOKEN)
    transport.WSS_HEADERS = dict(transport.WSS_HEADERS,
        **{'Sec-WebSocket-Protocol':'synthesize'})
