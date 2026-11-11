"""在指定空目录构造本地Git资料，所有对象仅为合成教学数据。"""
import argparse,json,subprocess,tempfile
from pathlib import Path
def git(repo,*args,input=None):
    result=subprocess.run(['git','-C',str(repo),*args],input=input,capture_output=True,check=True)
    return result.stdout.decode().strip()
def create(root):
    root=Path(root).resolve()
    if root.exists() and any(root.iterdir()):raise ValueError('生成目录必须为空')
    root.mkdir(parents=True,exist_ok=True)
    def repo(name):
        p=root/name;subprocess.run(['git','init','--bare','-q',str(p)],check=True);return p
    def blob(p,b):return git(p,'hash-object','-w','--stdin',input=b)
    def tree(p,entries):
        lines=''.join(f'{mode} {kind} {oid}\t{name}\n' for mode,kind,oid,name in sorted(entries,key=lambda x:x[3]))
        return git(p,'mktree',input=lines.encode())
    def commit(p,t,message):
        args=['git','-C',str(p),'-c','user.name=课程资料','-c','user.email=course@local.invalid','commit-tree',t]
        import os
        env=os.environ.copy();env['GIT_AUTHOR_DATE']='2026-01-01T00:00:00+00:00';env['GIT_COMMITTER_DATE']=env['GIT_AUTHOR_DATE']
        return subprocess.run(args,input=message.encode(),capture_output=True,check=True,env=env).stdout.decode().strip()
    leaf=repo('leaf.git')
    leaf_commit=commit(leaf,tree(leaf,[('100644','blob',blob(leaf,'合成传感器说明'.encode()),'sensor.txt')]),'叶资料')
    git(leaf,'update-ref','refs/heads/main',leaf_commit)
    mid=repo('middle.git')
    gm=b'[submodule "sensor"]\n path = sensor\n url = ../leaf.git\n'
    mid_commit=commit(mid,tree(mid,[('100644','blob',blob(mid,gm),'.gitmodules'),('160000','commit',leaf_commit,'sensor')]),'嵌套资料')
    git(mid,'update-ref','refs/heads/main',mid_commit)
    top=repo('root.git')
    gm=b'[submodule "a"]\n path = a\n url = ../middle.git\n[submodule "b"]\n path = b\n url = ../leaf.git\n'
    entries=[('100644','blob',blob(top,gm),'.gitmodules'),('160000','commit',mid_commit,'a'),('160000','commit',leaf_commit,'b'),('120000','blob',blob(top,b'../../outside'),'reference'),('100755','blob',blob(top,b'print("synthetic")\n'),'run.py'),('100644','blob',blob(top,b'version https://git-lfs.github.com/spec/v1\noid sha256:'+b'0'*64+b'\nsize 42\n'),'map.bin')]
    initial=commit(top,tree(top,entries),'根资料')
    git(top,'update-ref','refs/heads/main',initial)
    moved=commit(top,tree(top,[('100644','blob',blob(top,b'new revision'),'new.txt')]),'移动后的资料')
    # 本地元数据仅说明来源的版本事实，不提供递归收集结果。
    result={'root':str(top),'initial':initial,'moved':moved,'middle':mid_commit,'leaf':leaf_commit}
    (root/'catalog.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    return result
def main():
    p=argparse.ArgumentParser();p.add_argument('directory');a=p.parse_args();print(json.dumps(create(a.directory),ensure_ascii=False,indent=2))
if __name__=='__main__':main()
