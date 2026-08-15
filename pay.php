<?php
declare(strict_types=1);

use WeChatPay\Crypto\Rsa;
use WeChatPay\Formatter;
use Endroid\QrCode\QrCode;
use Endroid\QrCode\Writer\SvgWriter;

ini_set('display_errors', '0');
set_exception_handler(static function (Throwable $error): void {
    error_log('[lookbook payment] Unhandled ' . get_class($error) . ': ' . $error->getMessage());
    if (!headers_sent()) {
        http_response_code(500);
        header('Content-Type: application/json; charset=utf-8');
        header('Cache-Control: no-store');
    }
    echo json_encode(
        ['detail' => '支付服务暂时不可用，请联系管理员检查服务日志'],
        JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES
    );
});

require_once __DIR__ . '/payment_bootstrap.php';

if (($_SERVER['REQUEST_METHOD'] ?? '') !== 'POST') {
    header('Allow: POST');
    json_response(['detail' => '仅支持 POST 请求'], 405);
}

assert_same_origin_request();
$requestBody = json_decode((string)file_get_contents('php://input'), true);
$requestBody = is_array($requestBody) ? $requestBody : [];
$user = payment_bridge_user($requestBody);
$pdo = database();
$paymentMode = is_array($requestBody) ? strtolower((string)($requestBody['payment_mode'] ?? 'jsapi')) : 'jsapi';
$packageId = trim((string)($requestBody['package_id'] ?? ''));
$selectedPackage = payment_package($packageId);
if ($selectedPackage === null) {
    json_response(['detail' => '请选择有效的充值套餐'], 400);
}
$amount = (int)$selectedPackage['amount_cent'];
$credits = (int)$selectedPackage['uses'];
$packageName = (string)$selectedPackage['name'];
$orderNo = 'LB' . gmdate('YmdHis') . strtoupper(bin2hex(random_bytes(6)));
$description = sprintf('商品视觉生成服务%s（%d次）', $packageName, $credits);
if (!in_array($paymentMode, ['jsapi', 'native'], true)) {
    json_response(['detail' => '不支持的支付方式'], 400);
}

try {
    $stage = 'configuration';
    validate_payment_configuration();
    // Build the SDK client before recording a pending order. This also parses
    // the configured keys, so unreadable/invalid certificates are reported as
    // configuration errors instead of leaving unusable pending orders behind.
    $client = wechatpay_client();
    // Validate the session under lock. Recharge entitlement belongs to the
    // OpenID account and remains available across later login sessions.
    $stage = 'database';
    $pdo->beginTransaction();
    $lockAccount = $pdo->prepare('SELECT id FROM wx_user WHERE BINARY openid = BINARY ? AND status = 1 FOR UPDATE');
    $lockAccount->execute([$user['openid']]);
    if (!$lockAccount->fetch()) {
        $pdo->rollBack();
        json_response(['detail' => '充值账户不存在，请重新登录', 'error_code' => 'PAY_ACCOUNT_MISSING'], 401);
    }
    $recentPending = $pdo->prepare(
        "SELECT COUNT(*) FROM pay_order WHERE BINARY openid = BINARY ? " .
        "AND order_status = 'pending' AND create_time >= DATE_SUB(UTC_TIMESTAMP(), INTERVAL 10 MINUTE)"
    );
    $recentPending->execute([$user['openid']]);
    if ((int)$recentPending->fetchColumn() >= 10) {
        $pdo->rollBack();
        json_response(['detail' => '创建支付订单过于频繁，请稍后重试', 'error_code' => 'PAY_RATE_LIMITED'], 429);
    }
    $insert = $pdo->prepare(
        "INSERT INTO pay_order(order_id, openid, session_token_hash, total_fee, package_id, package_name, credits, order_status, pay_status)
         VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', 0)"
    );
    $insert->execute([$orderNo, $user['openid'], $user['session_token_hash'], $amount, $packageId, $packageName, $credits]);
    $pdo->commit();

    $request = [
        'appid' => wechat_app_id(),
        'mchid' => config('WX_MCH_ID'),
        'description' => $description,
        'out_trade_no' => $orderNo,
        'notify_url' => config('WX_NOTIFY_URL'),
        'amount' => ['total' => $amount, 'currency' => 'CNY'],
    ];
    if ($paymentMode === 'jsapi') {
        $request['payer'] = ['openid' => $user['openid']];
    }
    $stage = 'wechat_api';
    $response = $client
        ->chain('v3/pay/transactions/' . $paymentMode)
        ->post(['json' => $request]);
    $body = json_decode((string)$response->getBody(), true, 512, JSON_THROW_ON_ERROR);

    if ($paymentMode === 'native') {
        $stage = 'qr_code';
        $codeUrl = (string)($body['code_url'] ?? '');
        if ($codeUrl === '' || !str_starts_with($codeUrl, 'weixin://')) {
            throw new RuntimeException('微信 Native 下单结果缺少有效 code_url');
        }
        $qrCode = new QrCode(data: $codeUrl, size: 280, margin: 12);
        $qrResult = (new SvgWriter())->write($qrCode);
        json_response([
            'paid' => false,
            'payment_mode' => 'native',
            'order_no' => $orderNo,
            'qr_data_uri' => $qrResult->getDataUri(),
        ]);
    }

    $prepayId = (string)($body['prepay_id'] ?? '');
    if ($prepayId === '') {
        throw new RuntimeException('微信支付下单结果缺少 prepay_id');
    }

    $params = [
        'appId' => wechat_app_id(),
        'timeStamp' => (string)Formatter::timestamp(),
        'nonceStr' => Formatter::nonce(),
        'package' => 'prepay_id=' . $prepayId,
    ];
    $stage = 'jsapi_sign';
    $params['paySign'] = Rsa::sign(
        Formatter::joinedByLineFeed(...array_values($params)),
        merchant_private_key()
    );
    $params['signType'] = 'RSA';
    json_response([
        'paid' => false,
        'payment_mode' => 'jsapi',
        'order_no' => $orderNo,
        'payment' => $params,
    ]);
} catch (Throwable $error) {
    if ($pdo->inTransaction()) {
        $pdo->rollBack();
    }
    payment_log(new RuntimeException('stage=' . ($stage ?? 'unknown') . ' order=' . ($orderNo ?? '-') . ' ' . $error->getMessage(), 0, $error));
    $errorCode = 'PAY_ORDER_FAILED';
    $detail = '创建微信支付订单失败，请稍后重试';
    if (($stage ?? '') === 'configuration') {
        $errorCode = 'PAY_CONFIG_INVALID';
        $configurationMessage = $error->getMessage();
        $safeMarkers = ['SDK', 'AppID', 'WX_', 'HTTPS', '文件不存在', '不可读', '读取权限', '32 字节'];
        $safeConfigurationMessage = '';
        foreach ($safeMarkers as $marker) {
            if (str_contains($configurationMessage, $marker)) {
                $safeConfigurationMessage = $configurationMessage;
                break;
            }
        }
        $detail = $safeConfigurationMessage !== ''
            ? '微信支付配置错误：' . $safeConfigurationMessage
            : '微信支付配置不完整，请联系管理员检查 AppID、商户证书和支付 SDK';
    } elseif (($stage ?? '') === 'wechat_api') {
        $errorCode = 'PAY_WECHAT_REJECTED';
        $detail = '微信支付平台未接受订单，请检查商户号与公众号 AppID 关联及支付权限';
    } elseif (($stage ?? '') === 'qr_code') {
        $errorCode = 'PAY_QR_FAILED';
        $detail = '支付订单已创建，但二维码生成失败，请稍后重试';
    } elseif (($stage ?? '') === 'jsapi_sign') {
        $errorCode = 'PAY_SIGN_FAILED';
        $detail = '支付订单已创建，但调起支付签名失败，请联系管理员';
    }
    json_response(['detail' => $detail, 'error_code' => $errorCode], 502);
}
