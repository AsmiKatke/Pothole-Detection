document.addEventListener('DOMContentLoaded', function() {
    const ctx = document.getElementById('severityChart');
    if (ctx) {
        new Chart(ctx, {
            type: 'doughnut',
            data: {
                labels: ['Low', 'Medium', 'High (Critical)'],
                datasets: [{
                    label: 'Pothole Severity',
                    data: [45, 25, 12],
                    backgroundColor: [
                        'rgba(54, 162, 235, 0.8)',
                        'rgba(255, 206, 86, 0.8)',
                        'rgba(255, 107, 139, 0.8)' // primary pink
                    ],
                    borderColor: 'rgba(255,255,255,0.1)',
                    borderWidth: 2
                }]
            },
            options: {
                responsive: true,
                plugins: {
                    legend: {
                        position: 'bottom',
                        labels: { color: '#f0f0f0' }
                    }
                }
            }
        });
    }

    const trendCtx = document.getElementById('trendChart');
    if (trendCtx) {
        new Chart(trendCtx, {
            type: 'line',
            data: {
                labels: ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'],
                datasets: [{
                    label: 'Detections',
                    data: [12, 19, 15, 25, 22, 30, 28],
                    borderColor: '#ff6b8b',
                    backgroundColor: 'rgba(255, 107, 139, 0.2)',
                    borderWidth: 2,
                    fill: true,
                    tension: 0.4
                }]
            },
            options: {
                responsive: true,
                scales: {
                    y: { ticks: { color: '#f0f0f0' }, grid: { color: 'rgba(255,255,255,0.05)' } },
                    x: { ticks: { color: '#f0f0f0' }, grid: { color: 'rgba(255,255,255,0.05)' } }
                },
                plugins: {
                    legend: { display: false }
                }
            }
        });
    }
});
