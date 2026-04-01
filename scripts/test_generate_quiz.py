import requests

url = 'http://localhost:5500/generate-quiz-batch'
payload = {
    'topic_id': 'test',
    'topic_name': 'Test',
    'subject_name': 'Test',
    'num_questions': 2,
    'difficulty': 'Medium'
}

res = requests.post(url, json=payload)
print('status', res.status_code)
print('headers', res.headers)
print('body:', res.text)
