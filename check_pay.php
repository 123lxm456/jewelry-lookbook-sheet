<?php
declare(strict_types=1);

require_once __DIR__ . '/payment_bootstrap.php';

function require_paid_user(): array
{
    return current_wechat_user(true);
}

// Allows Nginx/PHP-FPM health checks and future PHP business endpoints to use
// this file directly. Python generation endpoints enforce the same condition.
if (realpath($_SERVER['SCRIPT_FILENAME'] ?? '') === __FILE__) {
    $user = require_paid_user();
    json_response([
        'ok' => true,
        'pay_status' => $user['pay_status'],
        'service_status' => $user['service_status'],
        'balance_cent' => $user['balance_cent'],
        'remaining_uses' => $user['remaining_uses'],
        'job_id' => $user['service_job_id'],
    ]);
}
