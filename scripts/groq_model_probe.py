from dotenv import load_dotenv
import os, groq

load_dotenv()

key = os.getenv('GROQ_API_KEY')
print('GROQ_API_KEY loaded:', bool(key))

cli = groq.Groq(api_key=key)
print('client type:', type(cli))

# Try to inspect available attributes
print('has models attr:', hasattr(cli, 'models'))
if hasattr(cli, 'models'):
    m = getattr(cli, 'models')
    print('models type:', type(m))
    print('models dir sample:', [x for x in dir(m) if 'list' in x or 'get' in x or 'info' in x][:50])

# Attempt to call a possible list or info method
for fn in ['list', 'list_models', 'listAvailable', 'get', 'get_model', 'info', 'describe']:
    if hasattr(cli, fn):
        print('found method', fn)
        try:
            res = getattr(cli, fn)()
            print(fn, '->', type(res), 'len?', getattr(res, '__len__', lambda: None)())
        except Exception as err:
            print(fn, 'call error:', err)

# If models attribute exists, try to list via it
if hasattr(cli, 'models'):
    m = cli.models
    if hasattr(m, 'list'):
        try:
            res = m.list()
            print('models.list() returned type', type(res))
            print('sample', res[:5])
        except Exception as e:
            print('models.list() error:', e)
