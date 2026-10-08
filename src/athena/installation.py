"""Configuration and pairing shared by the packaged applications."""
import base64
import json
import os
from pathlib import Path
import secrets
import ipaddress
from urllib.parse import urlsplit

def home():
    return Path(os.environ.get('ATHENA_APP_HOME') or Path(os.environ.get('LOCALAPPDATA', Path.home()))/'ATHENA')

def read_config():
    try: return json.loads((home()/'configuration.json').read_text(encoding='utf-8'))
    except (OSError, ValueError): return {}

def save_config(values):
    root=home(); root.mkdir(parents=True,exist_ok=True)
    path=root/'configuration.json'; temporary=path.with_suffix('.tmp')
    temporary.write_text(json.dumps(values,indent=2),encoding='utf-8')
    temporary.chmod(0o600); temporary.replace(path)

def environment(values=None):
    values=read_config() if values is None else values
    root=home()
    result={str(k):str(v) for k,v in values.items() if k.isupper() and isinstance(v,(str,int))}
    result.update(ATHENA_DATA_DIR=str(root/'data'), ATHENA_DATABASE_PATH=str(root/'data/athena.db'),
                  ATHENA_AUDIO_STATUS_PATH=str(root/'data/audio-status.json'),
                  ATHENA_TTS_BACKEND='edge', ATHENA_EDGE_VOICE='en-US-AvaNeural',
                  ATHENA_STT_BACKEND='qwen', ATHENA_REMOTE_AUDIO='0', ATHENA_WEB_HOST='127.0.0.1')
    return result

def defaults(values=None):
    values=dict(values or {})
    for name,size in [('ATHENA_LOCAL_CONTROL_TOKEN',48),('ATHENA_PC_TRANSFER_KEY',48),('ATHENA_WEB_SECRET',48)]:
        values.setdefault(name,secrets.token_urlsafe(size))
    values.setdefault('ATHENA_WEB_PASSWORD',secrets.token_urlsafe(12))
    values.setdefault('ATHENA_TIMEZONE','UTC')
    values.setdefault('ATHENA_PC_UPLOAD_URL','http://127.0.0.1:8781/upload')
    return values

def pairing_code(values):
    return 'ATHENA1.'+base64.urlsafe_b64encode(json.dumps(values,separators=(',',':')).encode()).decode().rstrip('=')

def decode_pairing(code,role):
    if not code.startswith('ATHENA1.') or len(code)>8192: raise ValueError('Paste a complete ATHENA pairing code.')
    try:
        payload=code[8:]; value=json.loads(base64.b64decode(payload+'='*(-len(payload)%4),altchars=b'-_',validate=True))
    except (ValueError,UnicodeError): raise ValueError('That pairing code is damaged.') from None
    if not isinstance(value,dict) or value.get('role')!=role: raise ValueError('That code is for a different setup step.')
    if role=='pc':
        url=urlsplit(value.get('url',''))
        try:
            address=ipaddress.ip_address(url.hostname)
            valid=address.version==4 and any(address in ipaddress.ip_network(net) for net in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
        except (ValueError,TypeError): valid=False
        if not valid or url.scheme!='http' or url.path!='/upload' or url.username or url.password or not isinstance(value.get('key'),str) or len(value['key'])<32:
            raise ValueError('Invalid PC receiver settings.')
    else:
        try:
            address=ipaddress.ip_address(value.get('host',''))
            valid=address.version==4 and any(address in ipaddress.ip_network(net) for net in ('10.0.0.0/8','172.16.0.0/12','192.168.0.0/16'))
            fingerprint=value.get('fingerprint','')
            valid=valid and isinstance(fingerprint,str) and len(fingerprint)==64 and len(bytes.fromhex(fingerprint))==32
        except (ValueError,TypeError):valid=False
        if not valid: raise ValueError('Invalid device address or certificate.')
    return value

def certificate():
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes,serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID
    from datetime import datetime,timedelta,timezone
    root=home(); root.mkdir(parents=True,exist_ok=True)
    cert_path=root/'localhost.crt'; key_path=root/'localhost.key'
    if cert_path.exists() and key_path.exists(): return cert_path,key_path
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'ATHENA')]); now=datetime.now(timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(days=1)).not_valid_after(now+timedelta(days=3650)).add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost'),x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]),False).sign(key,hashes.SHA256())
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,serialization.PrivateFormat.PKCS8,serialization.NoEncryption())); key_path.chmod(0o600)
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path,key_path
