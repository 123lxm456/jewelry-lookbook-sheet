<?php
declare(strict_types=1);

use WeChatPay\Builder;
use WeChatPay\Crypto\Rsa;

const LOOKBOOK_ROOT = __DIR__;
const LOOKBOOK_SESSION_COOKIE = 'lookbook_session';

function load_project_env(): void
{
    $path = LOOKBOOK_ROOT . '/.env';
    if (!is_file($path) || !is_readable($path)) {
        throw new RuntimeException('支付服务配置文件不存在');
    }
    foreach (file($path, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) ?: [] as $line) {
        $line = trim($line);
        if ($line === '' || str_starts_with($line, '#') || !str_contains($line, '=')) {
            continue;
        }
        [$name, $value] = explode('=', $line, 2);
        $name = trim($name);
        if (!preg_match('/^[A-Z][A-Z0-9_]*$/', $name) || getenv($name) !== false || isset($_ENV[$name]) || isset($_SERVER[$name])) {
            continue;
        }
        $value = trim($value);
        if (strlen($value) >= 2 && (($value[0] === '"' && str_ends_with($value, '"')) || ($value[0] === "'" && str_ends_with($value, "'")))) {
            $value = substr($value, 1, -1);
        }
        $_ENV[$name] = $value;
        $_SERVER[$name] = $value;
        if (function_exists('putenv')) {
            putenv($name . '=' . $value);
        }
    }
}

function config(string $name, ?string $default = null): string
{
    $value = getenv($name);
    if ($value === false || $value === '') {
        $value = $_ENV[$name] ?? $_SERVER[$name] ?? false;
    }
    if ($value === false || $value === '') {
        if ($default !== null) {
            return $default;
        }
        throw new RuntimeException('缺少配置：' . $name);
    }
    return $value;
}

function absolute_project_path(string $path): string
{
    if ($path === '') {
        throw new RuntimeException('证书路径不能为空');
    }
    return str_starts_with($path, '/') ? $path : LOOKBOOK_ROOT . '/' . ltrim($path, '/');
}

function payment_credential_path(string $name, ?string $default = null): string
{
    $configured = config($name, $default);
    $primary = absolute_project_path($configured);
    $candidates = [$primary];

    // PHP-FPM may retain an old absolute environment value after a deployment
    // directory changes. Only fall back to a credential with the same basename
    // in this application's cert directory; never search arbitrary directories.
    $basename = basename(str_replace('\\', '/', $configured));
    if ($basename !== '' && $basename !== '.' && $basename !== '..') {
        $fallback = LOOKBOOK_ROOT . '/cert/' . $basename;
        if (!in_array($fallback, $candidates, true)) {
            $candidates[] = $fallback;
        }
    }
    foreach ($candidates as $candidate) {
        clearstatcache(true, $candidate);
        if (is_file($candidate) && is_readable($candidate)) {
            return $candidate;
        }
    }

    $diagnostics = array_map(static function (string $candidate): array {
        return [
            'path' => $candidate,
            'exists' => is_file($candidate),
            'readable' => is_readable($candidate),
            'owner' => is_file($candidate) ? fileowner($candidate) : null,
            'perms' => is_file($candidate) ? substr(sprintf('%o', fileperms($candidate)), -4) : null,
        ];
    }, $candidates);
    $runtimeUser = function_exists('posix_geteuid') ? posix_geteuid() : null;
    error_log('[lookbook payment] credential=' . $name . ' euid=' . json_encode($runtimeUser) .
        ' candidates=' . json_encode($diagnostics, JSON_UNESCAPED_SLASHES));
    throw new RuntimeException($name . ' 文件不存在或 PHP-FPM 用户无读取权限');
}

function payment_packages(): array
{
    static $packages = null;
    if (is_array($packages)) {
        return $packages;
    }
    $path = LOOKBOOK_ROOT . '/config/payment_packages.json';
    if (!is_file($path) || !is_readable($path)) {
        throw new RuntimeException('充值套餐配置文件不存在或不可读');
    }
    $decoded = json_decode((string)file_get_contents($path), true, 32, JSON_THROW_ON_ERROR);
    if (!is_array($decoded) || $decoded === []) {
        throw new RuntimeException('充值套餐配置不能为空');
    }
    $packages = [];
    foreach ($decoded as $package) {
        if (!is_array($package)) {
            throw new RuntimeException('充值套餐配置格式无效');
        }
        $id = trim((string)($package['id'] ?? ''));
        $uses = (int)($package['uses'] ?? 0);
        $amount = (int)($package['amount_cent'] ?? 0);
        if ($id === '' || isset($packages[$id]) || $uses < 1 || $amount < 1 || $amount % $uses !== 0) {
            throw new RuntimeException('充值套餐 ID、次数或金额无效');
        }
        $package['id'] = $id;
        $package['uses'] = $uses;
        $package['amount_cent'] = $amount;
        $packages[$id] = $package;
    }
    return $packages;
}

function payment_package(string $packageId): ?array
{
    return payment_packages()[$packageId] ?? null;
}

function json_response(array $payload, int $status = 200): void
{
    http_response_code($status);
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    echo json_encode($payload, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
    exit;
}

function payment_log(Throwable $error): void
{
    error_log('[lookbook payment] ' . get_class($error) . ': ' . $error->getMessage());
}

function composer_autoload_path(): string
{
    $path = LOOKBOOK_ROOT . '/vendor/autoload.php';
    if (!is_file($path) || !is_readable($path)) {
        throw new RuntimeException('微信支付 SDK 加载文件不存在或不可读：vendor/autoload.php');
    }
    return $path;
}

function database(): PDO
{
    static $pdo = null;
    if ($pdo instanceof PDO) {
        return $pdo;
    }
    if (strtolower(config('DB_DRIVER', 'mysql')) !== 'mysql') {
        throw new RuntimeException('PHP 微信支付服务仅支持生产 MySQL 数据库');
    }
    $dsn = sprintf(
        'mysql:host=%s;port=%d;dbname=%s;charset=%s',
        config('DB_HOST'),
        (int)config('DB_PORT', '3306'),
        config('DB_NAME'),
        config('DB_CHARSET', 'utf8mb4')
    );
    $pdo = new PDO($dsn, config('DB_USER'), config('DB_PASSWORD'), [
        PDO::ATTR_ERRMODE => PDO::ERRMODE_EXCEPTION,
        PDO::ATTR_DEFAULT_FETCH_MODE => PDO::FETCH_ASSOC,
        PDO::ATTR_EMULATE_PREPARES => false,
    ]);
    return $pdo;
}

function current_wechat_user(bool $paidRequired = false): array
{
    // Browsers can retain an older path-scoped cookie alongside the current
    // root cookie. Test every value so a stale /jewelry-lookbook-sheet cookie
    // cannot shadow the valid FastAPI session.
    $tokens = [];
    $rawCookie = (string)($_SERVER['HTTP_COOKIE'] ?? '');
    if (preg_match_all('/(?:^|;\s*)' . preg_quote(LOOKBOOK_SESSION_COOKIE, '/') . '=([^;]*)/', $rawCookie, $matches)) {
        foreach ($matches[1] as $value) {
            $decoded = rawurldecode((string)$value);
            if ($decoded !== '') {
                $tokens[] = $decoded;
            }
        }
    }
    $parsedToken = $_COOKIE[LOOKBOOK_SESSION_COOKIE] ?? '';
    if (is_string($parsedToken) && $parsedToken !== '') {
        $tokens[] = $parsedToken;
    }
    $tokens = array_values(array_unique($tokens));
    if ($tokens === []) {
        json_response(['detail' => '请先使用微信登录'], 401);
    }
    $statement = database()->prepare(
        "SELECT users.id, users.openid, users.status, sessions.token_hash AS session_token_hash
         FROM sessions
         JOIN users ON users.id = sessions.user_id
         WHERE sessions.token_hash = ? AND sessions.expires_at > ? AND users.status = 'active'
         LIMIT 1"
    );
    $user = false;
    foreach ($tokens as $token) {
        $statement->execute([hash('sha256', $token), gmdate('Y-m-d\TH:i:sP')]);
        $user = $statement->fetch();
        if ($user) {
            break;
        }
    }
    if (!$user) {
        json_response(['detail' => '登录状态已过期，请重新登录'], 401);
    }
    $authorization = database()->prepare(
        "SELECT balance_cent, use_credits FROM wx_user WHERE BINARY openid = BINARY ? AND status = 1 LIMIT 1"
    );
    $authorization->execute([$user['openid']]);
    $account = $authorization->fetch() ?: ['balance_cent' => 0, 'use_credits' => 0];
    $user['balance_cent'] = (int)$account['balance_cent'];
    $user['remaining_uses'] = (int)$account['use_credits'];
    $user['service_status'] = $user['remaining_uses'] > 0 ? 'paid' : 'unpaid';
    $user['service_job_id'] = null;
    $user['pay_status'] = $user['remaining_uses'] > 0 ? 'paid' : 'unpaid';
    if ($paidRequired && $user['pay_status'] !== 'paid') {
        json_response(['detail' => '请先完成支付后再使用生成功能'], 402);
    }
    return $user;
}

function base64url_decode_strict(string $value): string|false
{
    if ($value === '' || preg_match('/[^A-Za-z0-9_-]/', $value)) {
        return false;
    }
    $padding = (4 - strlen($value) % 4) % 4;
    return base64_decode(strtr($value, '-_', '+/') . str_repeat('=', $padding), true);
}

function payment_bridge_user(array $requestBody): array
{
    $authorization = trim((string)($requestBody['authorization'] ?? ''));
    $parts = explode('.', $authorization);
    if (count($parts) !== 2) {
        json_response(['detail' => '支付授权无效，请刷新页面后重试', 'error_code' => 'PAY_AUTH_INVALID'], 401);
    }
    [$encodedPayload, $encodedSignature] = $parts;
    $signature = base64url_decode_strict($encodedSignature);
    $payloadJson = base64url_decode_strict($encodedPayload);
    $secret = config('PAY_BRIDGE_SECRET', config('WECHAT_APP_SECRET'));
    $expected = hash_hmac('sha256', $encodedPayload, $secret, true);
    if ($signature === false || $payloadJson === false || !hash_equals($expected, $signature)) {
        json_response(['detail' => '支付授权校验失败，请重新登录', 'error_code' => 'PAY_AUTH_SIGNATURE'], 401);
    }
    try {
        $payload = json_decode($payloadJson, true, 16, JSON_THROW_ON_ERROR);
    } catch (Throwable) {
        json_response(['detail' => '支付授权格式无效，请重新登录', 'error_code' => 'PAY_AUTH_PAYLOAD'], 401);
    }
    $expiresAt = (int)($payload['exp'] ?? 0);
    $openid = trim((string)($payload['sub'] ?? ''));
    $sessionHash = trim((string)($payload['sid'] ?? ''));
    if (($payload['v'] ?? null) !== 1 || $expiresAt < time() || $expiresAt > time() + 600 ||
        $openid === '' || strlen($openid) > 128 || !preg_match('/^[a-f0-9]{64}$/', $sessionHash)) {
        json_response(['detail' => '支付授权已过期，请刷新页面后重试', 'error_code' => 'PAY_AUTH_EXPIRED'], 401);
    }
    $statement = database()->prepare(
        "SELECT users.id, users.openid, users.status FROM users
         JOIN sessions ON sessions.user_id = users.id
         JOIN wx_user ON BINARY wx_user.openid = BINARY users.openid
         WHERE BINARY users.openid = BINARY ? AND sessions.token_hash = ?
           AND sessions.expires_at > ? AND users.status = 'active' AND wx_user.status = 1
         LIMIT 1"
    );
    $statement->execute([$openid, $sessionHash, gmdate('Y-m-d\\TH:i:sP')]);
    $user = $statement->fetch();
    if (!$user) {
        json_response(['detail' => '支付账户不存在，请重新登录', 'error_code' => 'PAY_ACCOUNT_MISMATCH'], 401);
    }
    $user['session_token_hash'] = $sessionHash;
    return $user;
}

function wechat_app_id(): string
{
    $loginAppId = config('WECHAT_APP_ID');
    $payAppId = config('WX_APPID', $loginAppId);
    if (!hash_equals($loginAppId, $payAppId)) {
        throw new RuntimeException('微信登录 AppID 与微信支付 AppID 不一致');
    }
    return $loginAppId;
}

function validate_payment_configuration(): void
{
    composer_autoload_path();
    wechat_app_id();
    config('WX_MCH_ID');
    config('WX_PLATFORM_PUBLIC_KEY_ID');
    if (strlen(config('WX_API_V3_KEY')) !== 32) {
        throw new RuntimeException('WX_API_V3_KEY 必须为 32 字节');
    }
    $notifyUrl = config('WX_NOTIFY_URL');
    if (!str_starts_with($notifyUrl, 'https://')) {
        throw new RuntimeException('微信支付通知地址必须使用 HTTPS');
    }
    foreach (['WX_PRIVATE_KEY_PATH', 'WX_PLATFORM_PUBLIC_KEY_PATH'] as $name) {
        payment_credential_path($name);
    }
    $serial = getenv('WX_MCH_SERIAL_NO');
    if (!is_string($serial) || $serial === '') {
        payment_credential_path('WX_MCH_CERT_PATH', 'cert/apiclient_cert.pem');
    }
}

function assert_same_origin_request(): void
{
    $expected = rtrim(config('APP_PUBLIC_URL', 'https://picture.deedface.com'), '/');
    $origin = rtrim((string)($_SERVER['HTTP_ORIGIN'] ?? ''), '/');
    if ($origin !== '' && !hash_equals($expected, $origin)) {
        json_response(['detail' => '请求来源无效'], 403);
    }
    if (($_SERVER['HTTP_X_REQUESTED_WITH'] ?? '') !== 'XMLHttpRequest') {
        json_response(['detail' => '请求校验失败'], 403);
    }
}

function merchant_certificate_serial(): string
{
    $configured = getenv('WX_MCH_SERIAL_NO');
    if (is_string($configured) && $configured !== '') {
        return strtoupper($configured);
    }
    $certificate = file_get_contents(payment_credential_path('WX_MCH_CERT_PATH', 'cert/apiclient_cert.pem'));
    if ($certificate === false) {
        throw new RuntimeException('无法读取商户 API 证书');
    }
    $parsed = openssl_x509_parse($certificate);
    $serial = is_array($parsed) ? ($parsed['serialNumberHex'] ?? '') : '';
    if (!is_string($serial) || $serial === '') {
        throw new RuntimeException('无法解析商户 API 证书序列号');
    }
    return strtoupper($serial);
}

function platform_public_key_id(): string
{
    return config('WX_PLATFORM_PUBLIC_KEY_ID');
}

function platform_public_key()
{
    $path = getenv('WX_PLATFORM_PUBLIC_KEY_PATH');
    if (!is_string($path) || $path === '') {
        $path = config('WX_CERT_PATH', 'cert/pub_key.pem');
    }
    return Rsa::from(
        'file://' . payment_credential_path('WX_PLATFORM_PUBLIC_KEY_PATH', $path),
        Rsa::KEY_TYPE_PUBLIC
    );
}

function merchant_private_key()
{
    return Rsa::from(
        'file://' . payment_credential_path('WX_PRIVATE_KEY_PATH'),
        Rsa::KEY_TYPE_PRIVATE
    );
}

function wechatpay_client()
{
    $autoload = composer_autoload_path();
    require_once $autoload;
    return Builder::factory([
        'mchid' => config('WX_MCH_ID'),
        'serial' => merchant_certificate_serial(),
        'privateKey' => merchant_private_key(),
        'certs' => [platform_public_key_id() => platform_public_key()],
    ]);
}

load_project_env();
