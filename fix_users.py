import json, os

f = "users.json"
if os.path.exists(f):
    with open(f,"r",encoding="utf-8-sig") as fp:
        data = json.load(fp)
    
    # Видаляємо God і порожні записи
    GOD_ID = "575590315"
    clean = {}
    for k,v in data.items():
        if k == GOD_ID: continue  # Видаляємо God
        if not k or k == "None": continue  # Порожні
        clean[k] = v
    
    with open(f,"w",encoding="utf-8") as fp:
        json.dump(clean, fp, ensure_ascii=False, indent=2)
    
    print("users.json виправлено!")
    print("Залишилось юзерів:", len(clean))
    for k,v in clean.items():
        print(f"  {k}: {v.get('role','?') if isinstance(v,dict) else v}")
else:
    print("users.json не знайдено")
