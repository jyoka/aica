"""Resume Draft の契約: 履歴書と職務経歴書の共通の形(schema)と、電話・メール・郵便番号の決まり(pii)。

読み取り(resume_intake)、音声の下書き(voice_resume)、事業者の保存(provider_mcp)が同じものを使う。
provider_mcp にも voice_resume にも依存しない(voice_resume は provider_mcp を import しない決まり。HLD 3.2)。
"""
