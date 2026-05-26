# AI Slack Curator

学生AIスクール向けのSlack自動投稿スクリプトです。

現在、Slackへの定期投稿はCodexオートメーション側で実行しています。
GitHub Actionsの定期実行は二重投稿防止のため無効化しています。

## 投稿内容

- `news`: Codexオートメーションで毎日7:00頃、AIニュース朝報を `#aiニュース` に投稿
- `daily-video`: Codexオートメーションで平日7:00頃、10〜20分のAI動画を `#ai学習動画` に投稿
- `weekly-video`: Codexオートメーションで土曜7:00頃、長尺1本＋短尺4本の週次動画まとめを `#ai学習動画` に投稿

## 動画ソース

動画は検索で広く拾わず、以下の指定ソースに絞ります。

- Lex Fridman Podcast
- All-In Podcast
- TED / TEDx / TED-Ed
- Stanford Graduate School of Business / View From The Top
- World Economic Forum

## 必要な GitHub Secrets

- `GEMINI_API_KEY`: Gemini APIキー
- `YOUTUBE_API_KEY`: YouTube Data API v3キー
- `SLACK_WEBHOOK_NEWS`: `#aiニュース` のIncoming Webhook URL
- `SLACK_WEBHOOK_VIDEO`: `#ai学習動画` のIncoming Webhook URL

任意:

- `GEMINI_MODEL`: 省略時は `gemini-2.5-flash`

## ローカル確認

```bash
python3 ai_slack_curator/curator.py --mode news --dry-run
python3 ai_slack_curator/curator.py --mode daily-video --dry-run
python3 ai_slack_curator/curator.py --mode weekly-video --dry-run
```

## 品質を保つ仕組み

- 公式サイト、RSS、指定YouTubeチャンネル/プレイリストから候補を収集
- URL単位で重複排除
- `.ai-curator-state.json` に投稿済みURLを保存
- Geminiには選定と日本語要約だけを任せる
- 投稿前に禁止絵文字、先頭絵文字、URL、文字数を検証
- 不確かな日は無理に盛らず、投稿を短縮または休止
