"""
Find environmental outlets country by country, instead of remembering them.

The roster to date is curated — my recall of which outlets exist, which
plateaus and skews toward what is written about in English. This sweeps Google
News per country, in the local language, for environmental terms, and reports
which outlets are actually publishing on the subject there. Domains that
recur are then probed for their own feed, which carries full text.

    python -m pipeline.discover_sources                 # every country
    python -m pipeline.discover_sources --region middle_africa
    python -m pipeline.discover_sources --min-hits 3    # stricter

Writes discovered_sources.yaml: candidate entries to review and paste into
sources.yaml. Nothing is added automatically — every candidate still has to
pass verify_sources, and you still decide whether an outlet belongs.

WHAT THIS DOES NOT REACH, and why it is not a complete answer:
  - Countries where Google News is blocked or absent (China is the largest).
  - Outlets Google does not index: small NGO bulletins, community radio
    transcripts, Facebook-only publishers, print-only papers.
  - Paywalled outlets, whose feeds carry headlines without the sentence a
    fact needs.
  - Places where environmental reporting is dangerous and therefore published
    by diaspora outlets filed under another country.
Expect this to close most of the gap in countries with a functioning press,
and little of it where the press itself is the missing thing.
"""

import argparse
import re
import sys
import time
from collections import Counter
from pathlib import Path
from html import unescape
from urllib.parse import quote, urlsplit

import feedparser
import yaml

from .harvest import BROWSER_UA, fetch_feed_bytes
from .verify_sources import CANDIDATE_PATHS, _alternate_links

ROOT = Path(__file__).resolve().parent.parent
GNEWS = "https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={gl}:{lang}"

# country code -> (region, google language, google country, search terms)
# Terms are in the language the country's press publishes in, because an
# English query returns English-language outlets wherever it is run.
COUNTRIES = {
 "NG":("western_africa","en","NG"),
 "GH":("western_africa","en","GH"),
 "SN":("western_africa","fr","SN"),
 "CI":("western_africa","fr","CI"),
 "ML":("western_africa","fr","ML"),
 "BF":("western_africa","fr","BF"),
 "NE":("western_africa","fr","NE"),
 "CM":("middle_africa","fr","CM"),
 "CD":("middle_africa","fr","CD"),
 "CG":("middle_africa","fr","CG"),
 "GA":("middle_africa","fr","GA"),
 "TD":("middle_africa","fr","TD"),
 "AO":("middle_africa","pt","AO"),
 "KE":("eastern_africa","en","KE"),
 "TZ":("eastern_africa","sw","TZ"),
 "UG":("eastern_africa","en","UG"),
 "ET":("eastern_africa","am","ET"),
 "MZ":("eastern_africa","pt","MZ"),
 "MG":("eastern_africa","fr","MG"),
 "ZM":("eastern_africa","en","ZM"),
 "ZW":("eastern_africa","en","ZW"),
 "SO":("eastern_africa","so","SO"),
 "ZA":("southern_africa","en","ZA"),
 "NA":("southern_africa","en","NA"),
 "BW":("southern_africa","en","BW"),
 "EG":("northern_africa","ar","EG"),
 "MA":("northern_africa","ar","MA"),
 "DZ":("northern_africa","ar","DZ"),
 "TN":("northern_africa","ar","TN"),
 "SD":("northern_africa","ar","SD"),
 "US":("northern_america","en","US"),
 "CA":("northern_america","en","CA"),
 "MX":("central_america","es","MX"),
 "GT":("central_america","es","GT"),
 "HN":("central_america","es","HN"),
 "CR":("central_america","es","CR"),
 "PA":("central_america","es","PA"),
 "CU":("caribbean","es","CU"),
 "DO":("caribbean","es","DO"),
 "HT":("caribbean","fr","HT"),
 "TT":("caribbean","en","TT"),
 "JM":("caribbean","en","JM"),
 "BR":("south_america","pt","BR"),
 "AR":("south_america","es","AR"),
 "CL":("south_america","es","CL"),
 "PE":("south_america","es","PE"),
 "CO":("south_america","es","CO"),
 "BO":("south_america","es","BO"),
 "EC":("south_america","es","EC"),
 "VE":("south_america","es","VE"),
 "PY":("south_america","es","PY"),
 "GY":("south_america","en","GY"),
 "SR":("south_america","nl","SR"),
 "KZ":("central_asia","ru","KZ"),
 "UZ":("central_asia","ru","UZ"),
 "KG":("central_asia","ru","KG"),
 "TJ":("central_asia","ru","TJ"),
 "TM":("central_asia","ru","TM"),
 "CN":("eastern_asia","zh-CN","CN"),
 "JP":("eastern_asia","ja","JP"),
 "KR":("eastern_asia","ko","KR"),
 "MN":("eastern_asia","mn","MN"),
 "TW":("eastern_asia","zh-TW","TW"),
 "IN":("southern_asia","en","IN"),
 "PK":("southern_asia","ur","PK"),
 "BD":("southern_asia","bn","BD"),
 "LK":("southern_asia","en","LK"),
 "NP":("southern_asia","ne","NP"),
 "AF":("southern_asia","fa","AF"),
 "IR":("southern_asia","fa","IR"),
 "ID":("south_eastern_asia","id","ID"),
 "PH":("south_eastern_asia","en","PH"),
 "VN":("south_eastern_asia","vi","VN"),
 "TH":("south_eastern_asia","th","TH"),
 "MY":("south_eastern_asia","ms","MY"),
 "KH":("south_eastern_asia","km","KH"),
 "MM":("south_eastern_asia","my","MM"),
 "LA":("south_eastern_asia","lo","LA"),
 "TR":("western_asia","tr","TR"),
 "IQ":("western_asia","ar","IQ"),
 "SA":("western_asia","ar","SA"),
 "AE":("western_asia","ar","AE"),
 "IL":("western_asia","he","IL"),
 "GE":("western_asia","ka","GE"),
 "AM":("western_asia","hy","AM"),
 "AZ":("western_asia","az","AZ"),
 "RU":("eastern_europe","ru","RU"),
 "UA":("eastern_europe","uk","UA"),
 "PL":("eastern_europe","pl","PL"),
 "RO":("eastern_europe","ro","RO"),
 "BG":("eastern_europe","bg","BG"),
 "CZ":("eastern_europe","cs","CZ"),
 "HU":("eastern_europe","hu","HU"),
 "DE":("western_europe","de","DE"),
 "FR":("western_europe","fr","FR"),
 "NL":("western_europe","nl","NL"),
 "BE":("western_europe","nl","BE"),
 "AT":("western_europe","de","AT"),
 "CH":("western_europe","de","CH"),
 "ES":("southern_europe","es","ES"),
 "IT":("southern_europe","it","IT"),
 "PT":("southern_europe","pt","PT"),
 "GR":("southern_europe","el","GR"),
 "RS":("southern_europe","sr","RS"),
 "HR":("southern_europe","hr","HR"),
 "GB":("northern_europe","en","GB"),
 "IE":("northern_europe","en","IE"),
 "SE":("northern_europe","sv","SE"),
 "NO":("northern_europe","no","NO"),
 "DK":("northern_europe","da","DK"),
 "FI":("northern_europe","fi","FI"),
 "IS":("northern_europe","is","IS"),
 "EE":("northern_europe","et","EE"),
 "LT":("northern_europe","lt","LT"),
 "AU":("australia_nz","en","AU"),
 "NZ":("australia_nz","en","NZ"),
 "PG":("melanesia","en","PG"),
 "FJ":("melanesia","en","FJ"),
 "SB":("melanesia","en","SB"),
 "VU":("melanesia","en","VU"),
 "NC":("melanesia","fr","NC"),
 "WS":("polynesia","en","WS"),
 "TO":("polynesia","en","TO"),
 "PF":("polynesia","fr","PF"),
 "CK":("polynesia","en","CK"),
 "FM":("micronesia","en","FM"),
 "MH":("micronesia","en","MH"),
 "KI":("micronesia","en","KI"),
 "PW":("micronesia","en","PW"),
 "GL":("polar","da","GL"),
}


# Search vocabulary, by language. Four terms was a keyhole: it found the
# outlets covering mining and deforestation and missed everyone writing about
# fisheries, pesticides, waste, wildlife trade, land rights, air quality,
# drought or environmental court cases. Each language carries a broad term
# list, chunked into several queries per country so no single query is diluted
# by too many alternatives.
LANG_TERMS = {
 "en": ["pollution","contamination","deforestation","logging","mining","tailings",
        "oil spill","water scarcity","drought","flooding","wildfire","air quality",
        "biodiversity","wildlife","extinction","overfishing","fisheries",
        "pesticides","toxic waste","plastic","landfill","emissions","coal",
        "pipeline","dam","land rights","indigenous land","land grab",
        "conservation","protected area","environmental permit","climate"],
 "fr": ["pollution","contamination","déforestation","exploitation forestière",
        "exploitation minière","orpaillage","marée noire","pénurie d'eau",
        "sécheresse","inondation","incendie","qualité de l'air","biodiversité",
        "faune sauvage","extinction","surpêche","pêche","pesticides",
        "déchets toxiques","plastique","décharge","émissions","charbon",
        "oléoduc","barrage","droits fonciers","terres autochtones",
        "accaparement des terres","conservation","aire protégée","climat"],
 "es": ["contaminación","deforestación","tala","minería","relaves","derrame",
        "escasez de agua","sequía","inundación","incendio forestal",
        "calidad del aire","biodiversidad","fauna silvestre","extinción",
        "sobrepesca","pesca","agrotóxicos","plaguicidas","residuos tóxicos",
        "plástico","vertedero","emisiones","carbón","oleoducto","represa",
        "derechos territoriales","territorio indígena","despojo","conservación",
        "área protegida","clima"],
 "pt": ["poluição","contaminação","desmatamento","exploração madeireira",
        "mineração","garimpo","rejeitos","derramamento","escassez de água",
        "seca","enchente","incêndio","qualidade do ar","biodiversidade",
        "fauna","extinção","sobrepesca","pesca","agrotóxicos","resíduos tóxicos",
        "plástico","aterro","emissões","carvão","oleoduto","barragem",
        "direitos territoriais","terra indígena","grilagem","conservação",
        "área protegida","clima"],
 "ar": ["تلوث","إزالة الغابات","تعدين","تسرب نفطي","شح المياه","جفاف","فيضان",
        "حرائق","جودة الهواء","تنوع بيولوجي","حياة برية","انقراض","صيد جائر",
        "مبيدات","نفايات سامة","بلاستيك","انبعاثات","فحم","سد","حقوق الأرض",
        "محمية طبيعية","مناخ","بيئة"],
 "ru": ["загрязнение","вырубка лесов","добыча","хвостохранилище","разлив нефти",
        "нехватка воды","засуха","наводнение","пожары","качество воздуха",
        "биоразнообразие","дикая природа","вымирание","перелов","рыболовство",
        "пестициды","токсичные отходы","пластик","свалка","выбросы","уголь",
        "трубопровод","плотина","земельные права","заповедник","климат","экология"],
 "uk": ["забруднення","вирубка лісів","видобуток","розлив нафти","нестача води",
        "посуха","повінь","пожежі","якість повітря","біорізноманіття",
        "дика природа","вимирання","перелов","пестициди","токсичні відходи",
        "пластик","звалище","викиди","вугілля","гребля","заповідник","довкілля"],
 "zh-CN": ["污染","毁林","采矿","尾矿","漏油","缺水","干旱","洪水","野火","空气质量",
           "生物多样性","野生动物","灭绝","过度捕捞","渔业","农药","有毒废物",
           "塑料","垃圾填埋","排放","煤炭","管道","水坝","土地权","保护区","气候","生态"],
 "zh-TW": ["污染","毀林","採礦","漏油","缺水","乾旱","洪水","野火","空氣品質",
           "生物多樣性","野生動物","滅絕","過度捕撈","農藥","有毒廢棄物","塑膠",
           "排放","煤炭","水壩","保護區","氣候","生態"],
 "ja": ["汚染","森林伐採","鉱山","油流出","water不足","干ばつ","洪水","山火事",
        "大気質","生物多様性","野生生物","絶滅","乱獲","漁業","農薬","有害廃棄物",
        "プラスチック","埋立","排出","石炭","ダム","保護区","気候","環境"],
 "ko": ["오염","산림 파괴","광산","기름 유출","물 부족","가뭄","홍수","산불",
        "대기질","생물다양성","야생동물","멸종","남획","어업","농약","유독 폐기물",
        "플라스틱","매립","배출","석탄","댐","보호구역","기후","환경"],
 "id": ["pencemaran","deforestasi","penebangan","tambang","limbah tailing",
        "tumpahan minyak","krisis air","kekeringan","banjir","kebakaran hutan",
        "kualitas udara","keanekaragaman hayati","satwa liar","kepunahan",
        "penangkapan berlebih","pestisida","limbah beracun","plastik","emisi",
        "batu bara","bendungan","hak tanah","masyarakat adat","kawasan lindung","iklim"],
 "ms": ["pencemaran","penyahutanan","pembalakan","perlombongan","tumpahan minyak",
        "kekurangan air","kemarau","banjir","kebakaran hutan","kualiti udara",
        "biodiversiti","hidupan liar","kepupusan","racun perosak","sisa toksik",
        "plastik","pelepasan","arang batu","empangan","hak tanah","kawasan perlindungan","iklim"],
 "vi": ["ô nhiễm","phá rừng","khai thác gỗ","khai khoáng","tràn dầu","thiếu nước",
        "hạn hán","lũ lụt","cháy rừng","chất lượng không khí","đa dạng sinh học",
        "động vật hoang dã","tuyệt chủng","đánh bắt quá mức","thuốc trừ sâu",
        "chất thải độc hại","nhựa","khí thải","than","đập","quyền đất đai",
        "khu bảo tồn","khí hậu","môi trường"],
 "th": ["มลพิษ","ตัดไม้ทำลายป่า","เหมืองแร่","น้ำมันรั่ว","ขาดแคลนน้ำ","ภัยแล้ง",
        "น้ำท่วม","ไฟป่า","คุณภาพอากาศ","ความหลากหลายทางชีวภาพ","สัตว์ป่า",
        "สูญพันธุ์","ประมงเกินขนาด","ยาฆ่าแมลง","ขยะพิษ","พลาสติก","การปล่อยก๊าซ",
        "ถ่านหิน","เขื่อน","สิทธิที่ดิน","พื้นที่คุ้มครอง","ภูมิอากาศ","สิ่งแวดล้อม"],
 "tr": ["kirlilik","ormansızlaşma","maden","petrol sızıntısı","su kıtlığı",
        "kuraklık","sel","orman yangını","hava kalitesi","biyoçeşitlilik",
        "yaban hayatı","yok oluş","aşırı avlanma","balıkçılık","pestisit",
        "zehirli atık","plastik","emisyon","kömür","baraj","arazi hakları",
        "koruma alanı","iklim","çevre"],
 "de": ["Verschmutzung","Kontamination","Entwaldung","Abholzung","Bergbau",
        "Ölaustritt","Wassermangel","Dürre","Hochwasser","Waldbrand","Luftqualität",
        "Artenvielfalt","Wildtiere","Artensterben","Überfischung","Pestizide",
        "Sondermüll","Plastik","Deponie","Emissionen","Kohle","Pipeline",
        "Staudamm","Landrechte","Schutzgebiet","Klima","Umwelt"],
 "it": ["inquinamento","contaminazione","deforestazione","disboscamento",
        "attività mineraria","sversamento","scarsità idrica","siccità",
        "alluvione","incendio","qualità dell'aria","biodiversità","fauna selvatica",
        "estinzione","pesca eccessiva","pesticidi","rifiuti tossici","plastica",
        "discarica","emissioni","carbone","diga","aree protette","clima","ambiente"],
 "nl": ["vervuiling","verontreiniging","ontbossing","houtkap","mijnbouw",
        "olielekkage","watertekort","droogte","overstroming","bosbrand",
        "luchtkwaliteit","biodiversiteit","wilde dieren","uitsterven","overbevissing",
        "bestrijdingsmiddelen","gifafval","plastic","stortplaats","uitstoot",
        "steenkool","stuwdam","natuurgebied","klimaat","milieu"],
 "pl": ["zanieczyszczenie","wylesianie","wycinka","górnictwo","wyciek ropy",
        "niedobór wody","susza","powódź","pożar lasu","jakość powietrza",
        "bioróżnorodność","dzika przyroda","wymieranie","przełowienie","pestycydy",
        "odpady toksyczne","plastik","składowisko","emisje","węgiel","zapora",
        "obszar chroniony","klimat","środowisko"],
 "ro": ["poluare","contaminare","defrișări","exploatare forestieră","minerit",
        "deversare","criza apei","secetă","inundații","incendiu","calitatea aerului",
        "biodiversitate","faună sălbatică","extincție","pescuit excesiv","pesticide",
        "deșeuri toxice","plastic","groapă de gunoi","emisii","cărbune","baraj",
        "arie protejată","climă","mediu"],
 "el": ["ρύπανση","μόλυνση","αποψίλωση","υλοτομία","εξόρυξη","πετρελαιοκηλίδα",
        "λειψυδρία","ξηρασία","πλημμύρα","πυρκαγιά","ποιότητα αέρα","βιοποικιλότητα",
        "άγρια ζωή","εξαφάνιση","υπεραλίευση","φυτοφάρμακα","τοξικά απόβλητα",
        "πλαστικά","εκπομπές","λιγνίτης","φράγμα","προστατευόμενη περιοχή",
        "κλίμα","περιβάλλον"],
 "sv": ["föroreningar","avskogning","skogsavverkning","gruvdrift","oljeutsläpp",
        "vattenbrist","torka","översvämning","skogsbrand","luftkvalitet",
        "biologisk mångfald","vilda djur","utrotning","överfiske","bekämpningsmedel",
        "giftigt avfall","plast","deponi","utsläpp","kol","damm","markrättigheter",
        "naturreservat","klimat","miljö"],
 "no": ["forurensning","avskoging","hogst","gruvedrift","oljeutslipp","vannmangel",
        "tørke","flom","skogbrann","luftkvalitet","biologisk mangfold","villmark",
        "utryddelse","overfiske","plantevernmidler","giftig avfall","plast",
        "deponi","utslipp","kull","demning","verneområde","klima","miljø"],
 "da": ["forurening","skovrydning","skovhugst","minedrift","olieudslip","vandmangel",
        "tørke","oversvømmelse","skovbrand","luftkvalitet","biodiversitet",
        "vilde dyr","udryddelse","overfiskeri","pesticider","giftigt affald",
        "plast","losseplads","udledning","kul","dæmning","naturområde","klima","miljø"],
 "fi": ["saastuminen","metsäkato","hakkuut","kaivostoiminta","öljyvuoto","vesipula",
        "kuivuus","tulva","metsäpalo","ilmanlaatu","luonnon monimuotoisuus",
        "villieläimet","sukupuutto","ylikalastus","torjunta-aineet","myrkkyjäte",
        "muovi","kaatopaikka","päästöt","hiili","pato","suojelualue","ilmasto","ympäristö"],
 "hi": ["प्रदूषण","वनों की कटाई","खनन","तेल रिसाव","जल संकट","सूखा","बाढ़",
        "जंगल की आग","वायु गुणवत्ता","जैव विविधता","वन्यजीव","विलुप्ति",
        "अत्यधिक मछली पकड़ना","कीटनाशक","विषाक्त कचरा","प्लास्टिक","उत्सर्जन",
        "कोयला","बांध","भूमि अधिकार","संरक्षित क्षेत्र","जलवायु","पर्यावरण"],
 "bn": ["দূষণ","বন উজাড়","খনন","তেল ছড়িয়ে পড়া","পানি সংকট","খরা","বন্যা",
        "দাবানল","বায়ুর মান","জীববৈচিত্র্য","বন্যপ্রাণী","বিলুপ্তি","অতিরিক্ত মাছ ধরা",
        "কীটনাশক","বিষাক্ত বর্জ্য","প্লাস্টিক","নির্গমন","কয়লা","বাঁধ",
        "ভূমি অধিকার","সংরক্ষিত এলাকা","জলবায়ু","পরিবেশ"],
 "ur": ["آلودگی","جنگلات کی کٹائی","کان کنی","تیل کا اخراج","پانی کی قلت","خشک سالی",
        "سیلاب","جنگل کی آگ","ہوا کا معیار","حیاتیاتی تنوع","جنگلی حیات","معدومیت",
        "کیڑے مار ادویات","زہریلا فضلہ","پلاسٹک","اخراج","کوئلہ","ڈیم",
        "زمین کے حقوق","محفوظ علاقہ","موسمیاتی","ماحول"],
 "ne": ["प्रदूषण","वन विनाश","खानी","पानी अभाव","खडेरी","बाढी","डढेलो","वायु गुणस्तर",
        "जैविक विविधता","वन्यजन्तु","लोप","विषादी","विषाक्त फोहोर","प्लास्टिक",
        "उत्सर्जन","कोइला","बाँध","भूमि अधिकार","संरक्षित क्षेत्र","जलवायु","वातावरण"],
 "fa": ["آلودگی","جنگل‌زدایی","معدن","نشت نفت","کم‌آبی","خشکسالی","سیل","آتش‌سوزی",
        "کیفیت هوا","تنوع زیستی","حیات وحش","انقراض","صید بی‌رویه","آفت‌کش",
        "زباله سمی","پلاستیک","انتشار","زغال‌سنگ","سد","حقوق زمین",
        "منطقه حفاظت‌شده","اقلیم","محیط زیست"],
 "he": ["זיהום","כריתת יערות","כרייה","דליפת נפט","מחסור במים","בצורת","שיטפון",
        "שריפה","איכות אוויר","מגוון ביולוגי","חיות בר","הכחדה","דיג יתר",
        "חומרי הדברה","פסולת רעילה","פלסטיק","פליטות","פחם","סכר",
        "שמורת טבע","אקלים","סביבה"],
 "sw": ["uchafuzi","ukataji miti","uchimbaji madini","umwagikaji mafuta",
        "uhaba wa maji","ukame","mafuriko","moto wa msitu","ubora wa hewa",
        "bioanuwai","wanyamapori","kutoweka","uvuvi kupita kiasi","viuatilifu",
        "taka sumu","plastiki","uzalishaji","makaa ya mawe","bwawa",
        "haki za ardhi","eneo la hifadhi","tabianchi","mazingira"],
 "am": ["ብክለት","የደን ጭፍጨፋ","ማዕድን","የነዳጅ ፍሳሽ","የውሃ እጥረት","ድርቅ","ጎርፍ",
        "የደን እሳት","የአየር ጥራት","ብዝሃ ሕይወት","የዱር እንስሳት","መጥፋት","ፀረ ተባይ",
        "መርዛማ ቆሻሻ","ፕላስቲክ","ልቀት","የድንጋይ ከሰል","ግድብ","የመሬት መብት",
        "ጥብቅ ቦታ","የአየር ንብረት","አካባቢ"],
 "so": ["wasakhda","jarista kaymaha","macdanta","daadashada saliidda","biyo yaraan",
        "abaar","daad","dab kaymo","tayada hawada","noolaha kala duwan",
        "duurjoogta","baabba'","kalluumeysi xad dhaaf","sunta cayayaanka",
        "qashin sun ah","caag","qiiq","dhuxul","biyo-xidheen","xuquuqda dhulka",
        "deegaan ilaalin","cimilada","deegaanka"],
 "km": ["ការបំពុល","ការកាប់បំផ្លាញព្រៃឈើ","រ៉ែ","ការកំពប់ប្រេង","កង្វះទឹក",
        "គ្រោះរាំងស្ងួត","ទឹកជំនន់","អគ្គីភ័យព្រៃ","គុណភាពខ្យល់","ជីវចម្រុះ",
        "សត្វព្រៃ","ការផុតពូជ","នេសាទហួសកម្រិត","ថ្នាំសំលាប់សត្វល្អិត","សំណល់ពុល",
        "ប្លាស្ទិច","ការបំភាយ","ធ្យូងថ្ម","ទំនប់","សិទ្ធិដីធ្លី","តំបន់ការពារ",
        "អាកាសធាតុ","បរិស្ថាន"],
 "my": ["ညစ်ညမ်းမှု","သစ်တောပြုန်းတီးမှု","သတ္တုတွင်း","ရေနံယိုစိမ့်မှု","ရေရှားပါးမှု",
        "မိုးခေါင်","ရေကြီး","တောမီး","လေထုအရည်အသွေး","ဇီဝမျိုးစုံမျိုးကွဲ",
        "တောရိုင်းတိရစ္ဆာန်","မျိုးသုဉ်း","ပိုးသတ်ဆေး","အဆိပ်စွန့်ပစ်ပစ္စည်း",
        "ပလတ်စတစ်","ထုတ်လွှတ်မှု","ကျောက်မီးသွေး","ဆည်","မြေယာအခွင့်အရေး",
        "ထိန်းသိမ်းရေးနယ်မြေ","ရာသီဥတု","ပတ်ဝန်းကျင်"],
 "lo": ["ມົນລະພິດ","ການທຳລາຍປ່າໄມ້","ບໍ່ແຮ່","ນ້ຳມັນຮົ່ວ","ຂາດແຄນນ້ຳ","ໄພແຫ້ງແລ້ງ",
        "ນ້ຳຖ້ວມ","ໄຟໄໝ້ປ່າ","ຄຸນນະພາບອາກາດ","ຄວາມຫຼາກຫຼາຍທາງຊີວະພາບ","ສັດປ່າ",
        "ການສູນພັນ","ຢາຂ້າແມງໄມ້","ຂີ້ເຫຍື້ອພິດ","ພລາສຕິກ","ການປ່ອຍອາຍ","ຖ່ານຫີນ",
        "ເຂື່ອນ","ສິດທິທີ່ດິນ","ເຂດປ້ອງກັນ","ດິນຟ້າອາກາດ","ສິ່ງແວດລ້ອມ"],
 "mn": ["бохирдол","ойн доройтол","уул уурхай","газрын тосны асгаралт","усны хомсдол",
        "ган","үер","ойн түймэр","агаарын чанар","биологийн олон янз байдал",
        "зэрлэг амьтан","устах","хэт загасчлал","пестицид","хортой хаягдал",
        "хуванцар","ялгаралт","нүүрс","далан","газрын эрх","хамгаалалттай газар",
        "уур амьсгал","байгаль орчин"],
 "ka": ["დაბინძურება","ტყეების გაჩეხვა","მოპოვება","ნავთობის დაღვრა","წყლის დეფიციტი",
        "გვალვა","წყალდიდობა","ტყის ხანძარი","ჰაერის ხარისხი","ბიომრავალფეროვნება",
        "ველური ბუნება","გადაშენება","ჭარბი თევზჭერა","პესტიციდები","ტოქსიკური ნარჩენები",
        "პლასტიკი","ემისიები","ნახშირი","კაშხალი","მიწის უფლებები",
        "დაცული ტერიტორია","კლიმატი","გარემო"],
 "hy": ["աղտոտում","անտառահատում","հանքարդյունաբերություն","նավթի արտահոսք",
        "ջրի պակաս","երաշտ","ջրհեղեղ","անտառային հրդեհ","օդի որակ",
        "կենսաբազմազանություն","վայրի բնություն","անհետացում","գերձկնորսություն",
        "թունաքիմիկատներ","թունավոր թափոններ","պլաստիկ","արտանետումներ","ածուխ",
        "ամբարտակ","հողի իրավունքներ","արգելոց","կլիմա","շրջակա միջավայր"],
 "az": ["çirklənmə","meşələrin qırılması","mədən","neft sızması","su qıtlığı",
        "quraqlıq","daşqın","meşə yanğını","hava keyfiyyəti","biomüxtəliflik",
        "vəhşi təbiət","nəsli kəsilmə","həddindən artıq balıq ovu","pestisidlər",
        "zəhərli tullantılar","plastik","emissiyalar","kömür","bənd",
        "torpaq hüquqları","qoruq","iqlim","ətraf mühit"],
 "sr": ["загађење","крчење шума","рударство","изливање нафте","несташица воде",
        "суша","поплава","шумски пожар","квалитет ваздуха","биодиверзитет",
        "дивље животиње","изумирање","прекомерни риболов","пестициди",
        "токсични отпад","пластика","емисије","угаљ","брана","права на земљиште",
        "заштићено подручје","клима","животна средина"],
 "hr": ["onečišćenje","krčenje šuma","rudarstvo","izlijevanje nafte","nestašica vode",
        "suša","poplava","šumski požar","kvaliteta zraka","bioraznolikost",
        "divlje životinje","izumiranje","prekomjerni ribolov","pesticidi",
        "otrovni otpad","plastika","emisije","ugljen","brana","zemljišna prava",
        "zaštićeno područje","klima","okoliš"],
 "cs": ["znečištění","odlesňování","těžba","únik ropy","nedostatek vody","sucho",
        "povodeň","lesní požár","kvalita ovzduší","biodiverzita","divoká zvěř",
        "vymírání","nadměrný rybolov","pesticidy","toxický odpad","plast",
        "emise","uhlí","přehrada","pozemková práva","chráněné území",
        "klima","životní prostředí"],
 "hu": ["szennyezés","erdőirtás","bányászat","olajszennyezés","vízhiány","aszály",
        "árvíz","erdőtűz","levegőminőség","biodiverzitás","vadvilág","kihalás",
        "túlhalászat","peszticidek","mérgező hulladék","műanyag","kibocsátás",
        "szén","gát","földjogok","védett terület","éghajlat","környezet"],
 "bg": ["замърсяване","обезлесяване","добив","нефтен разлив","недостиг на вода",
        "суша","наводнение","горски пожар","качество на въздуха","биоразнообразие",
        "дива природа","изчезване","свръхулов","пестициди","токсични отпадъци",
        "пластмаса","емисии","въглища","язовир","поземлени права",
        "защитена територия","климат","околна среда"],
 "et": ["reostus","metsaraie","kaevandamine","naftaleke","veepuudus","põud",
        "üleujutus","metsapõleng","õhukvaliteet","elurikkus","metsloomad",
        "väljasuremine","ülepüük","pestitsiidid","mürgised jäätmed","plast",
        "heitmed","kivisüsi","tamm","maaõigused","kaitseala","kliima","keskkond"],
 "lt": ["tarša","miškų naikinimas","kasyba","naftos išsiliejimas","vandens trūkumas",
        "sausra","potvynis","miško gaisras","oro kokybė","biologinė įvairovė",
        "laukinė gamta","išnykimas","peržvejojimas","pesticidai","toksiškos atliekos",
        "plastikas","emisijos","anglys","užtvanka","žemės teisės",
        "saugoma teritorija","klimatas","aplinka"],
 "is": ["mengun","skógeyðing","náma","olíuleki","vatnsskortur","þurrkur","flóð",
        "skógareldur","loftgæði","líffræðilegur fjölbreytileiki","villt dýr",
        "útrýming","ofveiði","varnarefni","eitraður úrgangur","plast","losun",
        "kol","stífla","landréttindi","friðland","loftslag","umhverfi"],
}


# Country names in the query language. Google News has editions for perhaps
# forty countries; for the rest the gl parameter is silently ignored and the
# language's home edition answers instead. Sweeping CM, CD, CG, GA and TD
# returned byte-identical French metropolitan results five times, and Angola
# returned Brazilian outlets. Naming the country inside the query works
# whether or not an edition exists.
COUNTRY_NAMES = {
 "NG":"Nigeria","GH":"Ghana","SN":"Sénégal","CI":"Côte d'Ivoire","ML":"Mali",
 "BF":"Burkina Faso","NE":"Niger","CM":"Cameroun","CD":"RDC Congo",
 "CG":"Congo Brazzaville","GA":"Gabon","TD":"Tchad","AO":"Angola",
 "KE":"Kenya","TZ":"Tanzania","UG":"Uganda","ET":"ኢትዮጵያ","MZ":"Moçambique",
 "MG":"Madagascar","ZM":"Zambia","ZW":"Zimbabwe","SO":"Soomaaliya",
 "ZA":"South Africa","NA":"Namibia","BW":"Botswana","EG":"مصر","MA":"المغرب",
 "DZ":"الجزائر","TN":"تونس","SD":"السودان","US":"United States","CA":"Canada",
 "MX":"México","GT":"Guatemala","HN":"Honduras","CR":"Costa Rica","PA":"Panamá",
 "CU":"Cuba","DO":"República Dominicana","HT":"Haïti","TT":"Trinidad",
 "JM":"Jamaica","BR":"Brasil","AR":"Argentina","CL":"Chile","PE":"Perú",
 "CO":"Colombia","BO":"Bolivia","EC":"Ecuador","VE":"Venezuela","PY":"Paraguay",
 "GY":"Guyana","SR":"Suriname","KZ":"Казахстан","UZ":"Узбекистан",
 "KG":"Кыргызстан","TJ":"Таджикистан","TM":"Туркменистан","CN":"中国",
 "JP":"日本","KR":"한국","MN":"Монгол","TW":"台灣","IN":"India","PK":"پاکستان",
 "BD":"বাংলাদেশ","LK":"Sri Lanka","NP":"नेपाल","AF":"افغانستان","IR":"ایران",
 "ID":"Indonesia","PH":"Philippines","VN":"Việt Nam","TH":"ประเทศไทย",
 "MY":"Malaysia","KH":"កម្ពុជា","MM":"မြန်မာ","LA":"ລາວ","TR":"Türkiye",
 "IQ":"العراق","SA":"السعودية","AE":"الإمارات","IL":"ישראל","GE":"საქართველო",
 "AM":"Հայաստան","AZ":"Azərbaycan","RU":"Россия","UA":"Україна","PL":"Polska",
 "RO":"România","BG":"България","CZ":"Česko","HU":"Magyarország",
 "DE":"Deutschland","FR":"France","NL":"Nederland","BE":"België","AT":"Österreich",
 "CH":"Schweiz","ES":"España","IT":"Italia","PT":"Portugal","GR":"Ελλάδα",
 "RS":"Србија","HR":"Hrvatska","GB":"United Kingdom","IE":"Ireland",
 "SE":"Sverige","NO":"Norge","DK":"Danmark","FI":"Suomi","IS":"Ísland",
 "EE":"Eesti","LT":"Lietuva","AU":"Australia","NZ":"New Zealand",
 "PG":"Papua New Guinea","FJ":"Fiji","SB":"Solomon Islands","VU":"Vanuatu",
 "NC":"Nouvelle-Calédonie","WS":"Samoa","TO":"Tonga","PF":"Polynésie",
 "CK":"Cook Islands","FM":"Micronesia","MH":"Marshall Islands","KI":"Kiribati",
 "PW":"Palau","GL":"Grønland",
}

# Domains belonging to a country other than the one being swept. Metropolitan
# and regional heavyweights dominate every sweep in their language and are
# never the local outlet we are looking for.
FOREIGN = {
 "fr": r"(ouest-france|lemonde|lefigaro|liberation|franceinfo|france24|rfi|"
       r"francetvinfo|france3-regions|ici\.fr|20minutes|leparisien|sudouest|"
       r"ladepeche|nouvelobs|lexpress|rtbf|lesoir|rts\.ch|letemps)",
 "pt": r"(globo|uol\.com|terra\.com|folha|estadao|record\.pt|publico\.pt|"
       r"observador\.pt|sapo\.pt|r7\.com|ig\.com|gov\.br)",
 "es": r"(elpais|elmundo|abc\.es|lavanguardia|20minutos|eldiario\.es|"
       r"marca|rtve|infobae|clarin|lanacion\.com\.ar)",
 "en": r"(bbc\.|guardian|cnn\.|nypost|dailymail|independent\.co\.uk|"
       r"telegraph|usatoday|washingtonpost|forbes|newsweek)",
 "ru": r"(rbc\.ru|lenta\.ru|ria\.ru|tass\.|kommersant|vedomosti|gazeta\.ru)",
 "ar": r"(alarabiya|skynewsarabia|bbc\.com/arabic|france24\.com/ar)",
 "de": r"(spiegel|welt\.de|faz\.net|zeit\.de|bild\.de|sueddeutsche|orf\.at)",
}

# Government portals and press-release wires are not newsrooms.
INSTITUTIONAL = re.compile(
    r"(\.gov|\.gouv|gov\.|gob\.|\.gob$|\.go\.[a-z]{2}$|\.go\.[a-z]{2}/|"
    r"presidencia|ministerio|govern|government|"
    r"ministry|ministere|kementerian|prnewswire|businesswire|globenewswire|"
    r"einpresswire|openpr)", re.I)


def cctld(code):
    return "." + code.lower()


TERMS_PER_QUERY = 5


def queries_for(lang, country_name=None):
    """
    Chunk a language's vocabulary into several OR-queries, each pinned to the
    country. Without the country name the query returns whatever the language's
    home edition carries, which is how five Central African sweeps came back
    with Le Monde.
    """
    terms = LANG_TERMS.get(lang) or LANG_TERMS.get(lang.split("-")[0]) or LANG_TERMS["en"]
    out = []
    for i in range(0, len(terms), TERMS_PER_QUERY):
        chunk = " OR ".join(f'"{t}"' if " " in t else t
                            for t in terms[i:i + TERMS_PER_QUERY])
        out.append(f"({chunk}) {country_name}" if country_name else chunk)
    return out


# Domains that are aggregators, wires or already in the roster.
SKIP = re.compile(
    r"(google|youtube|facebook|twitter|x\.com|msn\.|yahoo|reddit|wikipedia|"
    r"mongabay|reuters|apnews|bloomberg|ft\.com|nytimes|wsj|economist)", re.I)

# International broadcasters and pan-regional titles. These report on a
# country without being its press, and the roster covers that layer already.
# Without this they crowd out the national outlets a sweep exists to find.
IGO_NGO = re.compile(
    r"(reliefweb|fao\.org|undp\.org|unep\.|unicef|worldbank|\.un\.org|"
    r"greenpeace|wwf\.|nature\.org|ifaw\.org|islandconservation|forumsec|"
    r"iucn\.org|conservation\.org|birdlife|oxfam|redcross|\.museum|"
    r"storymaps\.arcgis|globalvoices|pressreader|scribd)", re.I)

# Sites that are not news at all but surface on keyword matches.
JUNK = re.compile(
    r"(snowreport|volcanodiscovery|mdundo|hamropatro|ushmm|holocaust|"
    r"\.edu$|\.edu\.|\.ac\.[a-z]{2}$|\.ac\.[a-z]{2}/|encyclopedia|"
    r"businessinsider|petro-news|noticiasagricolas|reporteminero|"
    r"pescare|agroperu|magyarmezogazdasag|construction|realestate)", re.I)

INTERNATIONAL = re.compile(
    r"(^|\.)(dw\.com|rfi\.fr|bbc\.|voanews|voaafrique|voaportugues|"
    r"france24|tv5monde|jeuneafrique|allafrica|africanews|apanews|"
    r"aa\.com\.tr|sputnik|rt\.com|cgtn|xinhua|aljazeera|trtworld|"
    r"rfa\.org|rferl\.org|benarnews|irrawaddy\.com|"
    r"openedition|academia\.edu|researchgate|scholar)", re.I)


def sweep(code, meta, pause=0.8):
    region, hl, gl = meta
    hits = Counter()
    entries = []
    name = COUNTRY_NAMES.get(code)
    # Reject ccTLDs belonging to other countries: abc.net.au topped Vanuatu,
    # Solomon Islands and PNG; nrk.no and aftenposten.no topped Greenland.
    own = cctld(code)
    other_cctld = re.compile(r"\.(au|nz|fr|uk|us|ca|de|no|se|dk|nl|be|ch|at|"
                             r"ru|in|za|ke|ng|br|mx|es|it|pt|jp|cn)$", re.I)
    foreign = re.compile(FOREIGN.get(hl.split("-")[0], r"(?!x)x"), re.I)
    for q in queries_for(hl, name):
        url = GNEWS.format(q=quote(q), hl=hl, gl=gl, lang=hl.split("-")[0])
        try:
            entries += feedparser.parse(fetch_feed_bytes(url)).entries
        except Exception as exc:
            if not entries:
                return region, Counter(), str(exc)[:90]
        time.sleep(pause)
    for entry in entries:
        src = (entry.get("source", {}) or {}).get("href") or ""
        if not src:
            m = re.search(r" - ([^-]+)$", entry.get("title", ""))
            src = m.group(1).strip() if m else ""
        host = urlsplit(src).netloc.lower().lstrip("www.") if "//" in src else src
        if not host or SKIP.search(host) or INSTITUTIONAL.search(host) \
                or NOT_NEWS.search(host) or INTERNATIONAL.search(host) \
                or IGO_NGO.search(host) or JUNK.search(host):
            continue
        if foreign.search(host):
            continue          # a metropolitan outlet, not the local press
        if not host.endswith(own) and other_cctld.search(host):
            continue          # registered to a different country
        # A country-code domain is strong evidence the outlet is local, so
        # weight it above a .com that merely mentioned the country.
        hits[host] += 3 if host.endswith(cctld(code)) else 1
    return region, hits, None


BAD_FEED_PATH = re.compile(r"/(api|search|sitemap|amp)/?$", re.I)

# Directories, trade bodies, apps and aggregators that surface on environmental
# queries without being newsrooms. iqair appeared in six countries.
NOT_NEWS = re.compile(
    r"(iqair|accuweather|weather\.com|tripadvisor|\.or\.id$|\.or\.th$|"
    r"association|chamber|\bngo\b|linkedin|glassdoor|indeed|"
    r"construction-property|realestate|property)", re.I)


def gnews_site_feed(host, hl, gl):
    """
    A per-domain Google News feed, for outlets that publish none themselves.

    Carries headline and link but usually not full text, so items from these
    yield facts only when the headline itself contains one. Worth having: the
    alternative is that Ghana's main wire and Thailand's public broadcaster are
    absent entirely.
    """
    url = GNEWS.format(q=quote(f"site:{host}"), hl=hl, gl=gl,
                       lang=hl.split("-")[0])
    try:
        if len(feedparser.parse(fetch_feed_bytes(url)).entries) >= 3:
            return url
    except Exception:
        pass
    return None


def _usable(url):
    """A feed must parse, carry several entries, and have per-item links."""
    if BAD_FEED_PATH.search(urlsplit(url).path):
        return False
    try:
        parsed = feedparser.parse(fetch_feed_bytes(url))
    except Exception:
        return False
    entries = parsed.entries
    if len(entries) < 3:
        return False
    # Real article feeds link each item somewhere; index dumps often do not.
    return sum(1 for e in entries if e.get("link")) >= 3


def find_feed(host):
    """Probe an outlet for a real feed of its own."""
    base = f"https://{host}"
    try:
        r = fetch_feed_bytes(base)
        for cand in _alternate_links(r, base):
            if _usable(unescape(cand)):
                return unescape(cand)
    except Exception:
        pass
    for path in CANDIDATE_PATHS[:10]:
        if _usable(base + path):
            return base + path
    return None


NO_FEED = []


def run(region_filter=None, min_hits=2):
    # Outlets retired earlier for bot protection or dead feeds. Discovery
    # finds them again every sweep; skip rather than re-propose.
    RETIRED = {"premiumtimesng.com", "desmog.com", "frontiermyanmar.net",
               "addisstandard.com", "solomonstarnews.com", "fijitimes.com.fj",
               "thejakartapost.com", "nepalitimes.com", "bianet.org"}
    existing = RETIRED | {urlsplit(s["url"]).netloc.lower().lstrip("www.")
                for s in yaml.safe_load((ROOT / "sources.yaml").read_text())["sources"]
                if s["kind"] == "rss"}
    found, no_press = [], []

    for code, meta in COUNTRIES.items():
        if region_filter and meta[0] != region_filter:
            continue
        region, hits, err = sweep(code, meta)
        local = [h for h in hits if h.endswith(cctld(code))]
        if err:
            print(f"{code}  sweep failed: {err}")
            continue
        fresh = [(h, n) for h, n in hits.most_common(8)
                 if n >= min_hits and h not in existing]
        if not fresh:
            no_press.append(code)
            print(f"{code}  {len(hits)} outlets, none new above threshold")
            continue
        print(f"{code}  {len(hits)} outlets ({len(local)} on the national domain)")
        for host, n in fresh[:4]:
            feed = find_feed(host)
            via_gnews = False
            if not feed:
                feed = gnews_site_feed(host, meta[1], meta[2])
                via_gnews = bool(feed)
            mark = ("via Google News (headlines only)" if via_gnews
                    else feed or "no feed found")
            print(f"     {n:>2}x {host:<34} {mark}")
            if not feed:
                NO_FEED.append((host, n, code))
            if feed:
                feed = unescape(feed)
                entry = {"id": re.sub(r"[^a-z0-9]+", "-", host)[:40],
                         "name": host, "kind": "rss", "url": feed,
                         "tier": "journalism", "regions": [region],
                         "lang": meta[1].split("-")[0], "general_news": True}
                if via_gnews:
                    entry["headlines_only"] = True
                found.append(entry)

    seen, unique = set(), []
    for c in found:
        if c["name"] in seen:
            continue
        seen.add(c["name"])
        unique.append(c)
    found = unique

    out = ROOT / "discovered_sources.yaml"
    # Accumulate across runs: sweeping a second region used to erase the first.
    if out.exists():
        prior = (yaml.safe_load(out.read_text()) or {}).get("sources") or []
        have = {c["name"] for c in found}
        found = [c for c in prior if c["name"] not in have] + found
    out.write_text(yaml.safe_dump({"sources": found}, allow_unicode=True,
                                  sort_keys=False))
    print(f"\n{len(found)} candidate feeds -> {out.name}")
    if NO_FEED:
        print("\nHigh-volume outlets with NO usable feed — these need another"
              "\nroute (site scrape, or a Google News feed for that domain):")
        for host, n, code in sorted(NO_FEED, key=lambda x: -x[1])[:25]:
            print(f"  {n:>4}x  {code}  {host}")
    if no_press:
        print(f"no new indexed outlets for: {', '.join(no_press)}")
    print("Review, then paste into sources.yaml and run verify_sources.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--region")
    ap.add_argument("--min-hits", type=int, default=2)
    a = ap.parse_args()
    sys.exit(run(a.region, a.min_hits))
