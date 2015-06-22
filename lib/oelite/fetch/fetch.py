import re
import os
import hashlib
import shutil
import string

import oelite.fetch
import oelite.util
import local
import url
import git
import hg
import svn

FETCHERS = {
    "file"	: local.LocalFetcher,
    "http"	: url.UrlFetcher,
    "https"	: url.UrlFetcher,
    "ftp"	: url.UrlFetcher,
    "git"	: git.GitFetcher,
    "hg"	: hg.HgFetcher,
    "svn"	: svn.SvnFetcher,
}

uri_pattern = re.compile("(?P<scheme>[^:]*)://(?P<location>[^;]+)(;(?P<params>.*))?")

unpack_ext = (
    ("tar_gz",	(".tar.gz", ".tgz", ".tar.Z")),
    ("tar_bz2",	(".tar.bz2", ".tbz", ".tbz2")),
    ("tar_xz",	(".tar.xz", ".txz")),
    ("tar_lz",	(".tar.lz", ".tlz")),
    ("zip",	(".zip", ".jar")),
    ("gz",	(".gz", ".Z", ".z")),
    ("bz2",	(".bz2",)),
    ("xz",	(".xz",)),
    ("lz",	(".lz",)),
    )

class OEliteUri:

    def __init__(self, uri, d):
        self.uri = uri
        # Note, do not store reference to meta
        self.recipe = "%s:%s_%s"%(d.get("RECIPE_TYPE"),
                                  d.get("PN"), d.get("PV"))
        # Try to avoid readinng recipe meta-data here, as it might
        # change later on, so better to read the meta-data when
        # needed.  This is especially problematic with variables
        # depending on ${EXTRA_ARCH} which might be changed after all
        # parse when resolving task dependencies.
        self.ingredients = d.get("INGREDIENTS")
        self.isubdir = (d.get("INGREDIENTS_SUBDIR") or
                        os.path.basename(d.get("FILE_DIRNAME")))
        self.strict = d.get("SRC_URI_STRICT") or False
        if self.strict == "0":
            self.strict = False
        m = uri_pattern.match(uri)
        if not m:
            raise oelite.fetch.InvalidURI(uri, "not an URI at all")
        self.scheme = m.group("scheme")
        self.location = m.group("location")
        if not self.scheme:
            raise oelite.fetch.InvalidURI(uri, "no URI scheme")
        if not self.location:
            raise oelite.fetch.InvalidURI(uri, "no URI location")
        self.params = {}
        if m.group("params"):
            for param in (m.group("params") or "").split(";"):
                try:
                    name, value = param.split("=")
                except ValueError:
                    name = param
                    value = "1"
                self.params[name] = value
        try:
            self.isubdir = self.params["isubdir"]
        except KeyError:
            pass
        if not self.scheme in FETCHERS:
            raise oelite.fetch.InvalidURI(
                uri, "unsupported URI scheme: %s"%(self.scheme))
        self.fdepends = []
        self.fetcher = FETCHERS[self.scheme](self, d)
        self.init_unpack_params()
        self.add_unpack_fdepends(d)
        self.init_patch_params(d)
        self.add_patch_fdepends()
        self.mirrors = self.init_mirrors(d.get("MIRRORS"))
        self.premirrors = self.init_mirrors(d.get("PREMIRRORS"))
        self.whitelist = self.init_filter(d.get("URL_WHITELIST"))
        self.blacklist = self.init_filter(d.get("URL_BLACKLIST"))
        return

    def init_mirrors(self, all_mirrors):
        if not all_mirrors:
            return []
        assert isinstance(all_mirrors, basestring)
        url = "%s://%s"%(self.scheme, self.location)
        all_mirrors = all_mirrors.split("\n")
        all_mirrors = map(string.strip, all_mirrors)
        all_mirrors = filter(None, all_mirrors)
        all_mirrors = map(string.split, all_mirrors)
        if not all_mirrors:
            return []
        mirrors = []
        for mirror in all_mirrors:
            if len(mirror) != 2:
                print "Warning: skipping invalid mirror line:", \
                    " ".join(mirror)
                continue
            (src_uri, mirror_uri) = tuple(mirror)
            m = re.match(src_uri, url)
            if m:
                e = url.rfind('/')
                mirrors.append((mirror_uri, url[e:]))
        return mirrors

    def init_filter(self, filterstr):
        if not filterstr:
            return None
        assert isinstance(filterstr, basestring)
        return "|".join(filterstr.split())

    def compile_filter(self, filterre):
        if not filterre:
            return None
        return re.compile(filterre)

    def allow_url(self, url):
        try:
            blacklist = self.blacklist_re
        except AttributeError:
            blacklist = self.blacklist_re = self.compile_filter(self.blacklist)
        if blacklist and blacklist.match(url):
            return False
        try:
            whitelist = self.whitelist_re
        except AttributeError:
            whitelist = self.whitelist_re = self.compile_filter(self.whitelist)
        if whitelist and not whitelist.match(url):
            return False
        return True

    def __str__(self):
        url = "%s://%s"%(self.scheme, self.location)
        try:
            fetcher_url = self.fetcher.url
        except:
            return url
        if url == fetcher_url:
            return url
        else:
            return "%s %s"%(self.scheme, fetcher_url)

    def init_unpack_params(self):
        if not "localpath" in dir(self.fetcher):
            return
        if not "unpack" in self.params:
            for (unpack, exts) in unpack_ext:
                assert isinstance(exts, tuple)
                for ext in exts:
                    if self.fetcher.localpath.endswith(ext):
                        self.params["unpack"] = unpack
                        return
        elif self.params["unpack"] == "0":
            del self.params["unpack"]
        if not "unpack" in self.params:
            return
        if self.params["unpack"] == "zip":
            if "dos" in self.params and self.params["dos"] != "0":
                self.params["unpack"] += "_dos"
        return

    def add_unpack_fdepends(self, d):
        if not "unpack" in self.params or not self.params["unpack"]:
            return
        fdepends = d.get("UNPACK_CMD_FDEPENDS_" + self.params["unpack"])
        if fdepends:
            self.fdepends.extend(fdepends.split())
        return

    def init_patch_params(self, d):
        if not "localpath" in dir(self.fetcher):
            return
        if not "apply" in self.params:
            patchfile = self.fetcher.localpath
            try:
                unpack = self.params["unpack"] or None
                if unpack == "0":
                    unpack = None
            except KeyError:
                unpack = None
            if unpack and self.fetcher.localpath.endswith(unpack):
                patchfile = self.fetcher.localpath[-len(unpack):]
            if patchfile.endswith(".patch") or patchfile.endswith(".diff"):
                self.params["apply"] = "yes"
        elif not self.params["apply"] in ["yes", "y", "1"]:
            del self.params["apply"]
        if "apply" in self.params:
            patchsubdir = d.get("PATCHSUBDIR")
            if "subdir" in self.params:
                subdir = self.params["subdir"]
                if (subdir != patchsubdir and
                    not subdir.startswith(patchsubdir + "")):
                    subdir = os.path.join(patchsubdir, subdir)
            else:
                subdir = patchsubdir
            self.params["subdir"] = subdir
        if not "striplevel" in self.params:
            self.params["striplevel"] = 1
        if "patchdir" in self.params:
            raise Exception("patchdir URI parameter support not implemented")
        return

    def add_patch_fdepends(self):
        if not "apply" in self.params or not self.params["apply"]:
            return
        self.fdepends.append("native:quilt")
        return

    def signature(self):
        try:
            return self.fetcher.signature()
        except oelite.fetch.NoSignature as e:
            if self.strict:
                raise e
            try:
                url = self.fetcher.url
            except AttributeError:
                url = self.uri
            print "%s: no checksum known for %s"%(self.recipe, url)
            return ""

    def cache(self):
        if not "cache" in dir(self.fetcher):
            return True
        return self.fetcher.cache()

    def write_checksum(self, filepath):
        md5path = filepath + ".md5"
        checksum = hashlib.md5()
        with open(filepath) as f:
            checksum.update(f.read())
        with open(md5path, "w") as f:
            f.write(checksum.digest())

    def verify_checksum(self, filepath):
        md5path = filepath + ".md5"
        if not os.path.exists(md5path):
            return None
        checksum = hashlib.md5()
        with open(filepath) as f:
            checksum.update(f.read())
        with open(md5path) as f:
            return f.readline().strip() == checksum.digest()

    def fetch(self):
        if not "fetch" in dir(self.fetcher):
            return True
        print "Fetching", str(self)
        return self.fetcher.fetch()

    def unpack(self, d, cmd):
        if not "localpath" in dir(self.fetcher):
            return self.fetcher.unpack(d)
        if not self._unpack(d, cmd):
            return False
        if "verify_unpacked" in dir(self.fetcher):
            return self.fetcher.verify_unpacked()
        return True

    def _unpack(self, d, cmd):
        print "Unpacking", self.fetcher.localpath
        srcpath = os.getcwd()
        self.srcfile = None
        cwd = None
        if "subdir" in self.params:
            srcpath = os.path.join(srcpath, self.params["subdir"])
            oelite.util.makedirs(srcpath)
            cwd = os.getcwd()
            os.chdir(srcpath)
        try:
            if not cmd or not "unpack" in self.params:
                if os.path.isdir(self.fetcher.localpath):
                    shutil.rmtree(srcpath, True)
                    shutil.copytree(self.fetcher.localpath, self.srcpath(d))
                    return True
                else:
                    shutil.copy2(self.fetcher.localpath, self.srcpath(d))
                    return True
            if "unpack_to" in self.params:
                cmd = cmd%(self.fetcher.localpath, self.srcpath(d))
            else:
                cmd = cmd%(self.fetcher.localpath)
            return oelite.util.shcmd(cmd)
        finally:
            if cwd:
                os.chdir(cwd)

    def srcpath(self, d):
        srcdir = d.get("SRCDIR")
        if "subdir" in self.params:
            srcdir = os.path.join(srcdir, self.params["subdir"])
        if "unpack_to" in self.params:
            return os.path.join(srcdir, self.params["unpack_to"])
        else:
            return os.path.join(srcdir,
                                os.path.basename(self.fetcher.localpath))

    def patchpath(self, d):
        srcpath = self.srcpath(d)
        patchdir = d.get("PATCHDIR")
        assert srcpath.startswith(patchdir + "/")
        return srcpath[len(patchdir) + 1:]

    def patch(self, d):
        with open("%s/series"%(d.get("PATCHDIR")), "a") as series:
            series.write("%s -p%s\n"%(
                    self.patchpath(d), self.params["striplevel"]))
        cmd = ["quilt", "-v", "--quiltrc", d.get("QUILTRC"), "push"]
        return oelite.util.shcmd(cmd)

    def mirror(self, d, mirror):
        if not "localpath" in dir(self.fetcher):
            if not "mirror" in dir(self.fetcher):
                return True
            return self.fetcher.mirror(mirror)
        src = self.fetcher.localpath
        if not src.startswith(self.ingredients):
            return True
        dst = os.path.join(mirror, src[len(self.ingredients)+1:])
        if os.path.exists(dst):
            m = hashlib.md5()
            with open(src, "r") as srcfile:
                m.update(srcfile.read())
                src_md5 = m.hexdigest()
            m = hashlib.md5()
            with open(dst, "r") as dstfile:
                m.update(dstfile.read())
                dst_md5 = m.hexdigest()
            if src_md5 != dst_md5:
                print "Mirror inconsistency:", dst
                print "%s != %s"%(src_md5, dst_md5)
                return False
            return True
        mirrordir = os.path.dirname(dst)
        if not os.path.exists(mirrordir):
            os.makedirs(mirrordir)
        print "Mirroring", dst[len(mirror)+1:]
        shutil.copy(src, dst)
        return True


def patch_init(d):
    quiltrc = d.get("QUILTRC")
    patchdir = d.get("PATCHDIR")
    with open(quiltrc, "w") as quiltrcfile:
        quiltrcfile.write("QUILT_PATCHES=%s\n"%(patchdir))
    series = os.path.join(patchdir, "series")
    if os.path.exists(series):
        os.remove(series)
    s = d.get("S")
    os.chdir(s)
    if os.path.exists(".pc"):
        while os.path.exists(".pc/applied-patches"):
            if oelite.util.shcmd("quilt -v --quiltrc %s pop"%(quiltrc)):
                # FIXME: proper error handling
                raise Exception("quilt pop failed")
        if not os.path.exists(".pc/series") and not os.path.exists(".pc/.quilt_series"):
            # FIXME: proper error handling
            raise Exception("Bad quilt .pc dir")
