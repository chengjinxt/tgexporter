打包生成exe程序，主要支持以下两个命令
tgexporter listen --save-dir "E:\PerSourceCodeStore\github\chengjinxt\tgexporter\发布内容"
注：save-dir 会在后面的路径下创建对应日志的子目录，用于收集此次监听到的消息转换后的数据，如：20260718

tgexporter publish-wechat --article-dir ".\发布内容\20260731"
注：article-dir 指定文章所在的目录，只遍历直接子级即可，不用进入子目录，如果此目录下有超过8篇文章的情况下，则每次发布完成8篇后，则把已经发布的8篇（md及引用到的文件）移动到子目录下：如： 第1批 ，如果超过16篇，则创建 第2批，依次类推

tgexporter publish-wechat --account movie4k --article-dir ".\发布内容\4K影视屋\20260801"

信息来源网站查询
tgexporter stats-domainsstats-domains

一键流程（全天运行，监听新消息并在当前目录下的.\发布内容 生成对应日期下的目录，并生成文章，生成文章后，每满足8篇，就保存一直草稿，如果当天一直没满8篇，但有文章即大于等于一篇文章也在23点时生成公众号草稿，以后每天都重复相同的逻辑）
tgexporter --draft

发布打包命令
在项目根目录运行 .\scripts\build_exe.ps1




==========调试运行=====
cmd
cd /d E:\PerSourceCodeStore\github\chengjinxt\tgexporter
python tgexporter.py publish-wechat --article-dir ".\发布内容\20260729"

===============
可以检测下目前生成的相关md文件 E:\PerSourceCodeStore\github\chengjinxt\tgexporter\发布内容\20260728
目前发布一个问题，这次分享的频道信息和原来的有所不同，原来基本都是资讯，但这次主要是来自github的软件分享，请基于这种软件分享的情况，重新调整一下提取模板，
1.要保证标题中含有对应的软件名称及主要功能说明，如： Unlnk - 一个极简实用的 Windows 右键增强小工具   如图，看目前生成的标题都是不太对的
2.要找到对应的开源地址，重新整理一下里面的readme说明，最好找中文版本的
3.要做到图文并茂，如果有演示网站的也可以自动进行测试，最好能生成截图或视频
4.有视频的要把视频下载下来
5.可以基于最近发的几个软件分享文章进行测试验证，并重新生成到 E:\PerSourceCodeStore\github\chengjinxt\tgexporter\发布内容\20260728 目录下，原来的标题不对的可以删除掉


要替换的字符
用户呼吁“还我旧版”：安卓 、 iOS 版微软 365 削弱生产力属性遭海量一星差评.jpg
" 替换为 “
: 替换为 ：
/ 替换为 、
斜杠"/"在中文里通常没有一个单一的、直接对应的符号，它在不同语境下有不同的意义，例如表示“或”（例如：男/女）、“和”（例如：苹果/香蕉）或者用来分隔日期和时间。如果是在数学中，它可能表示分数或除法。 
英文的斜杠 / 在中文中通常可以根据具体语境替换为斜杠 /（表示并列或选择）、顿号 、（表示并列的词语）或分号 ；（表示并列的句子或短语）等符号。 最直接的替换符号是英文斜杠 /，尤其是在不影响逻辑的场合，或者直接使用对应的中文斜杠 /。 


