$body = @{ 
    topic_id = 'test'
    topic_name = 'Test'
    subject_name = 'Test'
    num_questions = 2
    difficulty = 'Medium'
} | ConvertTo-Json

$response = Invoke-RestMethod -Uri 'http://localhost:5500/generate-quiz-batch' -Method Post -ContentType 'application/json' -Body $body
$response | ConvertTo-Json -Depth 5
