#!/usr/bin/env python3

import requests, sys, hmac, base64, logging, os
from pathlib import Path
from hashlib import sha1
from datetime import datetime
from time import time, strftime, sleep, ctime, mktime, gmtime
from ipaddress import ip_network
from icalendar import Calendar, Event

localisp = [
    ip_network("198.51.100.0/24"),
    ip_network("203.0.113.0/24"),
    ip_network("2001:db8::/32"),
]

configs = {
    "filterprovider": {
        "endpoint": "https://s3.us-west-2.amazonaws.com",
        "ressource": "/somes3ressource",
        "secretKey": "optional aws secretKey",
        "secretAccessKey": "optional aws secretAccessKey",
    },
}

# maximum number of imported prefixes
maxprefixes = 2000

# the active configuration to use
active_service = "filterprovider"
# an optional calendar of time period when you want to block something
calendar = "https://ics.ecal.com/ecal-sub/652fee7b067541000dd389b7/English%20Premier%20League.ics"
# path to where to store the calendar
calpath="/opt/tvfilter/calendar.ics"

endpoint = configs[active_service]["endpoint"]
ressource = configs[active_service]["ressource"]
secretKey = configs[active_service]["secretKey"]
secretAccessKey = configs[active_service]["secretAccessKey"]

LOG = logging.getLogger("tvfilter")
LOG.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
if os.environ.get("DEBUG"):
    level = logging.DEBUG
else:
    level = logging.INFO

ch.setLevel(level)
formatter = logging.Formatter(
    ">>> %(asctime)s :: %(name)s :: %(levelname)s :: %(message)s"
)
ch.setFormatter(formatter)
LOG.addHandler(ch)

def islocalisp(net):
    for localispnet in localisp:
        if net.overlaps(localispnet):
            return True
    return False

def s3headers(endpoint,ressource,secretKey,secretAccessKey):
    date = strftime("%a, %d %b %Y %H:%M:%S GMT", gmtime())
    ret = {
            "Content-Type": "text/plain; charset=UTF-8",
            "x-amz-date": date,
    }
    if secretKey != "":
        sigString = "GET\n\ntext/plain; charset=UTF-8\n\nx-amz-date:%s\n%s" % (date, ressource)
        hashed = hmac.new(bytes(secretKey, 'latin-1'), bytes(sigString, 'latin-1'), sha1)
        signature = str(base64.encodebytes(hashed.digest()).decode('utf-8')).rstrip('\n')
        ret['Authorization'] = "AWS %s:%s" % (secretAccessKey, signature)
    return ret

def getlist(endpoint,ressource,secretKey,secretAccessKey,lastChecksum):
    headers = s3headers(endpoint,ressource,secretKey,secretAccessKey)
    try:
        r = requests.get("%s%s" % (endpoint, ressource), headers=headers)
    except Exception as e:
        LOG.error("fail to get the updated list: %s" % str(e))
        return ('error', ['error'])
    if r.status_code != 200:
        LOG.error("fail to get the updated list: request returned an error %d" % r.status_code)
        return ('error', ['error'])
    checksum = sha1(bytes(r.text, 'UTF-8')).hexdigest()
    backup = "/opt/tvfilter/file-%s.txt" % checksum
    if not Path(backup).is_file():
        f = open(backup, "w")
        f.write(r.text)
        f.close()
    now = datetime.now()
    if lastChecksum != checksum:
        os.symlink(backup, '/opt/tvfilter/file-%s.txt' % strftime("%Y%m%d%H%M%S", gmtime()))
    return (checksum, r.text.rstrip('\n').split('\n'))

def route_filter(routes):
    ok = []
    i = 0
    for route in routes:
        if route == '':
            continue
        try:
            net = ip_network(route)
        except Exception as e:
            LOG.error("invalid element in the list: %s" % (str(e)))
            continue
        if net.is_multicast:
            LOG.warning("attempt to add multicast address: %s" % route)
            continue
        if net.is_private:
            LOG.warning("attempt to add private address: %s" % route)
            continue
        if net.is_unspecified:
            LOG.warning("attempt to add unspecified address: %s" % route)
            continue
        if net.is_reserved:
            LOG.warning("attempt to add reserved address: %s" % route)
            continue
        if net.is_loopback:
            LOG.warning("attempt to add loopback address: %s" % route)
            continue
        if net.is_link_local:
            LOG.warning("attempt to add link local address: %s" % route)
            continue
        if net.version == 4 and net.prefixlen < 32:
            LOG.warning("attempt to add a big prefix: %s" % route)
            continue
        if net.version == 6 and net.prefixlen < 128:
            LOG.warning("attempt to add a big prefix: %s" % route)
            continue
        if islocalisp(net):
            LOG.warning("attempt to add a GVA prefix: %s" % route)
            continue
        ok.append(str(net))
    ok = sorted(set(ok))

    if len(ok) > maxprefixes:
        LOG.warning("max prefixes (%d) reached" % maxprefixes)
        return ok[:maxprefixes]
    return ok

def announce(command, prefix):
    LOG.info("%s %s" % (command, prefix))
    if ":" in prefix:
        print("%s route %s next-hop ::1" % (command, prefix))
    else:
        print("%s route %s next-hop self" % (command, prefix))
    sys.stdout.flush()


def file_age(path):
    if not os.path.isfile(path):
        return 2592000
    return time() - os.path.getmtime(path)

def check_calendar():
    if calendar == "":
        return True
    if file_age(calpath) > 3600:
        try:
            r=requests.get(calendar, allow_redirects=True)
            open(calpath, 'wb').write(r.content)
        except Exception as e:
            print("issue downloading calendar: %s", e)
            return False
    with open(calpath) as f:
        ics = Calendar.from_ical(f.read())
        for event in ics.walk('VEVENT'):
            st = mktime(event.decoded('dtstart').timetuple())
            et = mktime(event.decoded('dtend').timetuple())
            if time() > st and time() < et:
                LOG.info("Blocage en cours: %s" % event.decoded('summary').decode('utf-8'))
                return True
    return False

if __name__ == '__main__':
    current_list = []
    checksum=''
    while True:
        if check_calendar():
            checksum,new_list = getlist(endpoint,ressource,secretKey,secretAccessKey,checksum)
        else:
            LOG.info("No blocking operation at the moment")
            new_list = []

        if 'error' in new_list:
            sleep(30)
            continue

        # filter list content
        new_list = route_filter(new_list)
        i = 0
        j = 0
        while(i < len(current_list) or j < len(new_list)):
            if (i == len(current_list)):
                announce("announce", new_list[j])
                j+=1
                continue
            if (j == len(new_list)):
                announce("withdraw", current_list[i])
                i+=1
                continue
            if (current_list[i] == new_list[j]):
                i+=1
                j+=1
                continue
            if (current_list[i] < new_list[j]):
                announce("withdraw", current_list[i])
                i+=1
                continue
            if (current_list[i] > new_list[j]):
                announce("announce", new_list[j])
                j+=1
                continue
        current_list = new_list
        sleep(30)
