# qrshort - QRコード & 短縮URL ジェネレーター

URLを入力するだけでQRコードと短縮URLを同時に生成できるWebアプリです。

🔗 **https://zzzzico.click/qr/**

---

## 技術スタック

| カテゴリ | 技術 |
|----------|------|
| バックエンド | Python 3.11 / FastAPI / Mangum |
| インフラ | AWS Lambda / API Gateway / DynamoDB |
| DNS・ドメイン | Route53 / ACM (SSL) |
| IaC | AWS SAM (CloudFormation) |
| CI/CD | GitHub Actions + OIDC |
| QRコード生成 | qrcode / Pillow |

---

## 機能

- URLを入力してQRコードを即時生成
- 短縮URL生成（`zzzzico.click/qr/r/xxxxxx`）
- QRコード PNG ダウンロード
- 短縮URLのワンクリックコピー
- 同一URLには同じ短縮コードを再利用

---

## アーキテクチャ

```
ブラウザ
  ↓ HTTPS
Route53 (DNS)
  ↓
API Gateway (カスタムドメイン: zzzzico.click)
  ↓ /qr/* → BasePathMapping
Lambda (FastAPI + Mangum)
  ├── GET  /          → HTML配信
  ├── POST /shorten   → 短縮URL + QRコード生成
  └── GET  /r/{code} → 元のURLへリダイレクト
  ↓
DynamoDB
  └── code (PK) ← → original_url (GSI)
```

同一ドメイン（`zzzzico.click`）に複数アプリを相乗りさせる設計。
各アプリが独自の API Gateway ベースパスを持つ。

```
zzzzico.click/qr/      → このアプリ（qrshort）
zzzzico.click/XXX/     → 将来の別アプリ
```

---

## ファイル構成

```
qrshort/
├── .github/
│   └── workflows/
│       └── deploy.yml      # GitHub Actions CI/CD
├── templates/
│   └── index.html          # フロントエンド
├── main.py                 # FastAPI アプリ
├── requirements.txt        # Python 依存パッケージ
├── template.yaml           # SAM テンプレート (AWSリソース定義)
└── samconfig.toml          # SAM デプロイ設定
```

---

## セキュリティ

- `http` / `https` スキームのみ許可（`javascript:` 等をブロック）
- URL 長制限（2048文字）
- セキュリティヘッダー付与（`X-Frame-Options`, `CSP` 等）
- API Gateway スロットリング（10 req/秒）
- DynamoDB 保存データの暗号化 (SSE)
- GitHub Actions は OIDC 認証（アクセスキー不要）

---

## ローカル開発

```bash
# 依存パッケージインストール
pip install -r requirements.txt

# サーバー起動
uvicorn main:app --reload
```

http://localhost:8000 でアクセス（DynamoDB はローカル or AWS の認証情報が必要）

---

## デプロイ

`main` ブランチに push すると GitHub Actions が自動でデプロイします。

```bash
git add .
git commit -m "変更内容"
git push
```

手動でデプロイする場合:

```bash
sam build && sam deploy
```

---

## AWS コスト

| リソース | 無料枠 |
|----------|--------|
| Lambda | 月100万リクエスト |
| API Gateway | 月100万リクエスト（12ヶ月） |
| DynamoDB | 25GB 永久無料 |
| Route53 | ホストゾーン $0.50/月 |

ドメイン代（`zzzzico.click`）以外はほぼ無料で運用可能。
