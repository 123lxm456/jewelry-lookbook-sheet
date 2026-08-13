<?php
declare(strict_types=1);

use WeChatPay\Crypto\AesGcm;
use WeChatPay\Crypto\Rsa;
use WeChatPay\Formatter;

ini_set('display_errors', '0');
set_time_limit(300);

set_exception_handler(static function (Throwable $error): void {
    error_log('[lookbook payment notify] Unhandled ' . get_class($error) . ': ' . $error->getMessage());
    if (!headers_sent()) {
        http_response_code(500);
        header('Content-Type: application/json; charset=utf-8');
        header('Cache-Control: no-store');
    }
    echo json_encode(
        ['code' => 'FAIL', 'message' => '回调处理失败'],
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    );
});

require_once dirname(__DIR__) . '/payment_bootstrap.php';

function notify_header(string $name): string
{
    $serverName = 'HTTP_' . strtoupper(str_replace('-', '_', $name));
    return trim((string)($_SERVER[$serverName] ?? ''));
}

function notify_failure(string $message, int $status = 401): void
{
    json_response(['code' => 'FAIL', 'message' => $message], $status);
}

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    notify_failure('仅支持 POST 请求', 405);
}

try {
    $autoload = composer_autoload_path();
    require_once $autoload;

    $signature = notify_header('Wechatpay-Signature');
    $timestamp = notify_header('Wechatpay-Timestamp');
    $nonce = notify_header('Wechatpay-Nonce');
    $serial = notify_header('Wechatpay-Serial');
    $body = file_get_contents('php://input');
    if ($signature === '' || $timestamp === '' || $nonce === '' || $serial === '' || !is_string($body) || $body === '') {
        notify_failure('回调请求不完整');
    }
    if (!ctype_digit($timestamp) || abs(Formatter::timestamp() - (int)$timestamp) > 300) {
        notify_failure('回调时间戳无效');
    }
    if (!hash_equals(platform_public_key_id(), $serial)) {
        notify_failure('微信支付公钥标识不匹配');
    }
    $verified = Rsa::verify(
        Formatter::joinedByLineFeed($timestamp, $nonce, $body),
        $signature,
        platform_public_key()
    );
    if (!$verified) {
        notify_failure('回调签名验证失败');
    }

    $notification = json_decode($body, true, 512, JSON_THROW_ON_ERROR);
    if (($notification['event_type'] ?? '') !== 'TRANSACTION.SUCCESS') {
        json_response(['code' => 'SUCCESS', 'message' => '成功']);
    }
    $resource = $notification['resource'] ?? null;
    if (!is_array($resource) || ($resource['algorithm'] ?? '') !== 'AEAD_AES_256_GCM') {
        notify_failure('回调资源不存在');
    }
    if (strlen(config('WX_API_V3_KEY')) !== 32) {
        throw new RuntimeException('API v3 密钥必须为 32 字节');
    }
    $plaintext = AesGcm::decrypt(
        (string)($resource['ciphertext'] ?? ''),
        config('WX_API_V3_KEY'),
        (string)($resource['nonce'] ?? ''),
        (string)($resource['associated_data'] ?? '')
    );
    $payment = json_decode($plaintext, true, 512, JSON_THROW_ON_ERROR);
    if (($payment['trade_state'] ?? '') !== 'SUCCESS') {
        json_response(['code' => 'SUCCESS', 'message' => '成功']);
    }
    if (($payment['appid'] ?? '') !== wechat_app_id() || ($payment['mchid'] ?? '') !== config('WX_MCH_ID')) {
        notify_failure('商户身份不匹配');
    }

    $orderNo = (string)($payment['out_trade_no'] ?? '');
    $transactionId = (string)($payment['transaction_id'] ?? '');
    $payerOpenid = (string)($payment['payer']['openid'] ?? '');
    $tradeType = strtoupper((string)($payment['trade_type'] ?? ''));
    $paidAmount = (int)($payment['amount']['total'] ?? -1);
    $currency = (string)($payment['amount']['currency'] ?? '');
    if ($orderNo === '' || $transactionId === '' || !in_array($tradeType, ['JSAPI', 'NATIVE'], true)) {
        notify_failure('支付结果字段不完整');
    }

    $pdo = database();
    $pdo->beginTransaction();
    $select = $pdo->prepare('SELECT * FROM pay_order WHERE order_id = ? FOR UPDATE');
    $select->execute([$orderNo]);
    $order = $select->fetch();
    if (!$order) {
        $pdo->rollBack();
        notify_failure('订单不存在');
    }
    if (in_array(($order['order_status'] ?? ''), ['paid', 'processing', 'consumed'], true)) {
        $pdo->commit();
        json_response(['code' => 'SUCCESS', 'message' => '成功']);
    }
    if ((int)$order['total_fee'] !== $paidAmount || $currency !== 'CNY') {
        $pdo->rollBack();
        notify_failure('订单支付信息不匹配');
    }
    // New orders carry an immutable package snapshot. Orders created before
    // this deployment keep their historical one-cent-per-use entitlement.
    $purchasedUses = $order['credits'] === null ? (int)$order['total_fee'] : (int)$order['credits'];
    $packageId = $order['package_id'] === null ? 'legacy' : (string)$order['package_id'];
    if ($purchasedUses < 1 || $paidAmount < 1 || $paidAmount % $purchasedUses !== 0) {
        $pdo->rollBack();
        notify_failure('订单套餐快照无效');
    }
    if ($tradeType === 'JSAPI' && ($payerOpenid === '' || !hash_equals($order['openid'], $payerOpenid))) {
        $pdo->rollBack();
        notify_failure('JSAPI 付款用户与订单用户不匹配');
    }

    $duplicate = $pdo->prepare('SELECT order_id FROM pay_order WHERE transaction_id = ? AND order_id <> ? LIMIT 1');
    $duplicate->execute([$transactionId, $orderNo]);
    if ($duplicate->fetch()) {
        $pdo->rollBack();
        notify_failure('微信支付流水号已被使用');
    }

    try {
        $paidAt = (new DateTimeImmutable((string)($payment['success_time'] ?? 'now')))
            ->setTimezone(new DateTimeZone('UTC'))
            ->format('Y-m-d H:i:s');
    } catch (Throwable) {
        $paidAt = gmdate('Y-m-d H:i:s');
    }
    $updateOrder = $pdo->prepare(
        "UPDATE pay_order SET pay_status = 1, order_status = 'paid', transaction_id = ?, pay_time = ? " .
        "WHERE order_id = ? AND order_status = 'pending'"
    );
    $updateOrder->execute([$transactionId, $paidAt, $orderNo]);
    if ($updateOrder->rowCount() !== 1) {
        throw new RuntimeException('订单状态更新失败');
    }
    $insertLot = $pdo->prepare(
        "INSERT INTO credit_lot(order_id, openid, package_id, total_uses, remaining_uses, amount_cent, remaining_amount_cent, created_at) " .
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
    );
    $insertLot->execute([
        $orderNo, $order['openid'], $packageId, $purchasedUses, $purchasedUses,
        $paidAmount, $paidAmount, $paidAt,
    ]);
    // Amount and generation credits are credited atomically with the order and
    // lot ledger. Duplicate notifications return before reaching this block.
    $creditAccount = $pdo->prepare(
        "UPDATE wx_user SET balance_cent = balance_cent + ?, use_credits = use_credits + ?, " .
        "pay_status = 1, update_time = ? WHERE BINARY openid = BINARY ? AND status = 1"
    );
    $creditAccount->execute([$paidAmount, $purchasedUses, $paidAt, $order['openid']]);
    if ($creditAccount->rowCount() !== 1) {
        throw new RuntimeException('用户充值余额更新失败');
    }
    $pdo->commit();
    json_response(['code' => 'SUCCESS', 'message' => '成功']);
} catch (Throwable $error) {
    if (isset($pdo) && $pdo instanceof PDO && $pdo->inTransaction()) {
        $pdo->rollBack();
    }
    payment_log($error);
    notify_failure('回调处理失败', 500);
}
