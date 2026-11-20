import sqlite3
import json

def init_db(db_name="peypeak.db"):
    """データベースとテーブルの初期化"""
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    
    # テーブル作成（サイト名と案件名の組み合わせを一意にするユニーク制約）
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            site_name TEXT NOT NULL,
            project_name TEXT NOT NULL,
            highest_points INTEGER NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(site_name, project_name)
        )
    """)
    conn.commit()
    conn.close()
    print("Database initialized successfully.")

def upsert_project(site_name, project_name, current_points, db_name="peypeak.db"):
    """
    案件データを判定して挿入・更新（Upsert処理）
    過去最高値よりも高い場合のみ highest_points を更新する
    戻り値: (is_updated, old_points) -> (更新されたかどうかの真偽値, 更新前のポイント)
    """
    conn = sqlite3.connect(db_name)
    cursor = conn.cursor()
    
    # 挿入前に、現在のデータベース内の最高値を取得（旧値のトラッキング）
    cursor.execute("SELECT highest_points FROM projects WHERE site_name=? AND project_name=?", (site_name, project_name))
    row = cursor.fetchone()
    old_points = row[0] if row else 0 # 過去データがなければ0pt
    
    # SQLのON CONFLICT（衝突時）の制御構文を使用
    sql = """
        INSERT INTO projects (site_name, project_name, highest_points)
        VALUES (?, ?, ?)
        ON CONFLICT(site_name, project_name) DO UPDATE SET
            highest_points = MAX(projects.highest_points, excluded.highest_points),
            updated_at = CURRENT_TIMESTAMP
    """
    
    is_updated = False
    try:
        # 型のキャストと簡易バリデーション（ガードロジック）
        points = int(current_points)
        if points < 0 or points > 1000000: # 異常値フィルター
            raise ValueError(f"異常なポイント数を検知しました: {points}")
            
        cursor.execute(sql, (site_name, project_name, points))
        conn.commit()
        
        # 更新後の最高値を取得して、以前より高くなっていれば「更新成功」と判定
        cursor.execute("SELECT highest_points FROM projects WHERE site_name=? AND project_name=?", (site_name, project_name))
        new_points = cursor.fetchone()[0]
        
        if new_points > old_points:
            is_updated = True
            print(f"【DB更新】{old_points}pt -> {new_points}pt")
        else:
            print(f"【DB維持】{old_points}ptのまま（入力: {points}pt）")
            
    except ValueError as e:
        print(f"【バリデーションエラー】: {e}")
    finally:
        conn.close()
        
    return is_updated, old_points

def export_db_to_json(db_name="peypeak.db", json_file_name="data.json"):
    """
    【Week 3 新機能】
    SQLiteデータベース内の全案件データを取得し、
    フロントエンド用の静的ファイル（data.json）として書き出す（SSG化処理）。
    """
    conn = sqlite3.connect(db_name)
    # 検索結果をC言語のようなタプルではなく、Pythonの辞書型（キー名アクセス）で扱えるようにする設定
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # データベースから更新日時が新しい順に全件取得する
        cursor.execute("SELECT site_name, project_name, highest_points, updated_at FROM projects ORDER BY updated_at DESC")
        rows = cursor.fetchall()
        
        # 取得したバイナリデータを、Pythonのリスト＆辞書型へ詰め替える（構造体配列の作成に相当）
        project_list = []
        for row in rows:
            project_list.append({
                "site_name": row["site_name"],
                "project_name": row["project_name"],
                "highest_points": row["highest_points"],
                "updated_at": row["updated_at"]
            })
            
        # メモリ上のデータをJSONフォーマットのテキストファイルとして書き出す（不揮発化）
        # ensure_ascii=False を指定して日本語の文字化け（Unicodeエスケープ）を防止
        with open(json_file_name, "w", encoding="utf-8") as f:
            json.dump(project_list, f, ensure_ascii=False, indent=4)
            
        print(f"📁 静的JSONファイル出力完了: {json_file_name} に全 {len(project_list)} 件をエクスポートしました。")
        
    except Exception as e:
        print(f"【ファイル出力例外発生】: {e}")
    finally:
        conn.close()

# --- 単体テスト実行用（python db_manager.py で実行した時のみ動くルーチン） ---
if __name__ == "__main__":
    init_db()
    upsert_project("モッピー", "楽天カード", 10000)
    export_db_to_json() # 単体でのファイル書き出しテスト