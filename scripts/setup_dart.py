"""User-operated credential entry. No secret stdout, logs, or command-line values."""
import re
import threading
import tkinter as tk
from tkinter import ttk
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import httpx
from app.dart_credentials import save_key

def main():
    root=tk.Tk();root.title('스윙 연구 · OpenDART 연결');root.geometry('560x270');root.resizable(False,False)
    frame=ttk.Frame(root,padding=24);frame.pack(fill='both',expand=True)
    ttk.Label(frame,text='발급받은 OpenDART 인증키를 입력하세요',font=('맑은 고딕',13,'bold')).pack(anchor='w',pady=(0,12))
    ttk.Label(frame,text='키는 이 PC의 Windows 사용자 계정으로 암호화해 저장합니다.\n채팅으로 전송하지 않으며, DART 연결 확인에만 사용합니다.').pack(anchor='w')
    entry=ttk.Entry(frame,show='●',width=65);entry.pack(fill='x',pady=16);entry.focus_set()
    status=tk.StringVar(value='인증키 입력 후 연결 확인 및 저장을 눌러주세요.')
    ttk.Label(frame,textvariable=status,wraplength=505).pack(anchor='w')
    def done(message,ok=False):
        status.set(message);button.configure(state='normal')
        if ok:entry.delete(0,'end');entry.configure(state='disabled');button.configure(text='완료 · 창 닫기',command=root.destroy)
    def submit():
        key=entry.get().strip()
        if not re.fullmatch('[0-9a-fA-F]{40}',key):status.set('인증키 40자 전체를 복사했는지 확인해주세요.');return
        button.configure(state='disabled');status.set('OpenDART 연결 확인 중…')
        def work():
            try:
                # Official company endpoint. Never expose response bodies or URLs in errors.
                with httpx.Client(timeout=20) as client:
                    response=client.get('https://opendart.fss.or.kr/api/company.json',params={'crtfc_key':key,'corp_code':'00126380'})
                    response.raise_for_status();code=response.json().get('status')
                if code!='000':
                    messages={'010':'등록되지 않은 인증키입니다.','011':'사용할 수 없는 인증키입니다.','012':'허용 IP 설정을 확인해주세요.','020':'조회 한도를 초과했습니다. 나중에 다시 확인해주세요.'}
                    root.after(0,done,messages.get(code,'DART 승인 상태를 확인한 뒤 다시 시도해주세요.'));return
                save_key(Path(__file__).parent/'data',key)
                root.after(0,done,'연결 확인 및 암호화 저장 완료. 채팅에 “저장했어”라고 알려주세요.',True)
            except Exception:root.after(0,done,'연결 또는 저장에 실패했습니다. 네트워크와 인증키 승인 상태를 확인해주세요.')
        threading.Thread(target=work,daemon=True).start()
    button=ttk.Button(frame,text='연결 확인 및 저장',command=submit);button.pack(anchor='e',pady=12)
    root.bind('<Return>',lambda e:submit() if str(button['state'])!='disabled' and str(entry['state'])!='disabled' else None)
    root.mainloop()

if __name__=='__main__':main()
